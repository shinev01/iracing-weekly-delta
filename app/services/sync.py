"""Season-scoped, resumable synchronization orchestration."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, Protocol

from app.database.repository import RaceRepository
from app.models.normalized import RaceResult, normalize_category
from app.services.http import RateLimitError, SourceBlockedError
from app.services.iracingdata import (
    IRacingDataClient,
    IRacingDataRace,
    SeasonInfo,
    SeasonMetadataClient,
    season_identity,
)
from app.services.irstats import IrstatsClient, IrstatsRaceIndex


class ProgressReporter(Protocol):
    def __call__(self, message: str) -> None: ...


def console_progress(message: str) -> None:
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        # Windows console code pages may not represent provider error text.
        encoding = getattr(__import__("sys").stdout, "encoding", None) or "utf-8"
        safe = message.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(safe, flush=True)


@dataclass(slots=True)
class SyncReport:
    cust_id: int
    mode: str
    pages: int
    found_subsessions: int
    existing_locally: int
    details_requested: int
    imported: int
    skipped_existing: int
    failed: int
    stopped_reason: str | None = None
    completed_pages: int = 0
    total_pages: int | None = None
    irstats_requests: int = 0
    cached_pages: int = 0
    current_season_name: str | None = None
    current_season_ids: tuple[int, ...] = ()
    races_stored: int = 0


@dataclass(slots=True)
class _PageResult:
    page: Any
    from_cache: bool


class SyncService:
    """Coordinate source clients while keeping source logic out of the UI."""

    def __init__(
        self,
        repository: RaceRepository,
        *,
        ir_stats: IrstatsClient | None = None,
        iracing_data: IRacingDataClient | None = None,
        metadata: SeasonMetadataClient | None = None,
        detail_workers: int = 2,
        progress: ProgressReporter = console_progress,
    ) -> None:
        self.repository = repository
        self.ir_stats = ir_stats or IrstatsClient(progress=progress)
        self.iracing_data = iracing_data or IRacingDataClient()
        self.metadata = metadata or SeasonMetadataClient()
        # Kept as a compatibility setting for callers that configure it. The
        # season-scoped flow is deliberately sequential so it can stop at a
        # boundary before requesting the next page.
        self.detail_workers = max(1, min(detail_workers, 4))
        self.progress = progress

    def sync(
        self,
        cust_id: int,
        *,
        full_rescan: bool = False,
        season_key: str | None = None,
        rescan_season: bool = False,
    ) -> SyncReport:
        """Synchronize only the requested scope.

        ``full_rescan`` remains an explicit advanced escape hatch for the old
        entire-career workflow. Normal Sync always reads one fresh iRStats
        page. Initial setup and season backfills consume pages lazily and stop
        at the selected season boundary.
        """
        self.repository.set_setting("cust_id", str(cust_id))
        self._reset_driver_checkpoint_if_needed(cust_id)
        self.repository.set_meta("last_sync_error", None)
        existing = self.repository.existing_subsessions(cust_id)
        self._refresh_metadata_catalog()
        current = self._current_season()
        target = self._target_season(season_key)
        if current is not None:
            self.repository.set_meta("current_season_name", current.label)
            self.repository.set_meta("current_season_ids", list(current.season_ids))

        if full_rescan:
            mode = "career"
            start_page = self._full_rescan_start_page(cust_id)
            use_cache = True
            force_first_network = False
            max_pages = None
        elif target is not None:
            mode = "rescan-season" if rescan_season else "season"
            start_page = 1
            use_cache = True
            force_first_network = rescan_season and target.key == getattr(current, "key", None)
            max_pages = None
        elif not existing and current is not None:
            mode = "initial"
            target = current
            start_page = 1
            use_cache = True
            force_first_network = True
            max_pages = None
        else:
            mode = "quick"
            # A normal sync still inspects only page 1, but the current-season
            # identity prevents older races later on that page from being
            # mistaken for new races.
            target = current
            start_page = 1
            use_cache = False
            force_first_network = True
            max_pages = 1

        self.progress("Connecting to iRStats...")
        self.progress(f"Reading page {start_page}...")
        pages = 0
        found_ids: set[int] = set()
        details_requested = 0
        skipped_existing = 0
        imported = 0
        failed = 0
        irstats_requests = 0
        cached_pages = 0
        stopped_reason: str | None = None
        total_pages = _as_int(self.repository.get_meta("total_pages"))
        completed_pages = _as_int(self.repository.get_meta("last_successful_irstats_page")) or 0
        self._index_requests = 0

        try:
            for page_result in self._iter_index_pages(
                cust_id,
                start_page=start_page,
                max_pages=max_pages,
                use_cache=use_cache,
                force_first_network=force_first_network,
            ):
                page = page_result.page
                pages += 1
                if page_result.from_cache:
                    cached_pages += 1
                else:
                    irstats_requests = self._index_requests
                if page.total_pages is not None:
                    total_pages = page.total_pages
                    self.repository.set_meta("total_pages", total_pages)

                # The index page is durable before any iRacingData request.
                self.repository.upsert_irstats_page(cust_id, page)
                for race in page.races:
                    self.repository.upsert_irstats_index(cust_id, race, page.page)
                    found_ids.add(race.subsession_id)
                if mode == "career":
                    self.repository.set_meta("last_successful_irstats_page", page.page)
                    self.repository.set_meta("index_sync_incomplete", True)
                    completed_pages = page.page

                self.progress(f"Found {len(page.races)} races.")
                page_stats = self._process_page(
                    cust_id,
                    page.races,
                    target=target,
                    stop_before_target=(
                        target is not None
                        and current is not None
                        and target.key == current.key
                    ),
                )
                details_requested += page_stats[0]
                imported += page_stats[1]
                skipped_existing += page_stats[2]
                failed += page_stats[3]

                if target is not None and page_stats[4]:
                    break
                if mode == "quick":
                    break
                if page.has_more:
                    self.progress(f"Reading page {page.page + 1}...")
        except RateLimitError as exc:
            irstats_requests = self._index_requests
            stopped_reason = self._rate_limit_message(
                exc,
                completed_pages=completed_pages if mode == "career" else 0,
                total_pages=total_pages,
            )
            if mode == "career":
                self.repository.set_meta("index_sync_incomplete", True)
        except SourceBlockedError as exc:
            irstats_requests = self._index_requests
            stopped_reason = str(exc)
            if mode == "career":
                self.repository.set_meta("index_sync_incomplete", True)

        now = _now()
        self.repository.set_meta("last_irstats_sync", now)
        self.repository.set_meta("last_sync", now)
        self.repository.set_meta("last_sync_mode", mode)
        if mode == "career" and stopped_reason is None:
            self.repository.set_meta("index_sync_incomplete", False)
        if stopped_reason:
            self.repository.set_meta("last_sync_error", stopped_reason)
            self.progress(stopped_reason)
            self.progress("Import stopped; imported progress was saved.")
        else:
            self.progress("Sync complete.")
            self.progress(f"{imported} new races.")

        self.repository.set_meta("last_irstats_requests", irstats_requests)
        self.repository.set_meta("last_iracingdata_requests", details_requested)
        self.repository.set_meta("last_sync_imported", imported)
        self.repository.set_meta("last_sync_cached_pages", cached_pages)

        current_ids = tuple(current.season_ids) if current is not None else ()
        return SyncReport(
            cust_id=cust_id,
            mode=mode,
            pages=pages,
            found_subsessions=len(found_ids),
            existing_locally=len(existing),
            details_requested=details_requested,
            imported=imported,
            skipped_existing=skipped_existing,
            failed=failed,
            stopped_reason=stopped_reason,
            completed_pages=completed_pages,
            total_pages=total_pages,
            irstats_requests=irstats_requests,
            cached_pages=cached_pages,
            current_season_name=current.label if current is not None else None,
            current_season_ids=current_ids,
            races_stored=self.repository.count(cust_id),
        )

    def _process_page(
        self,
        cust_id: int,
        races: list[IrstatsRaceIndex],
        *,
        target: SeasonInfo | None,
        stop_before_target: bool = False,
    ) -> tuple[int, int, int, int, bool]:
        """Process one page before requesting the next one.

        Returns detail requests, imports, complete-detail skips, failures, and
        whether a selected season boundary was reached.
        """
        details_requested = imported = skipped_existing = failed = 0
        target_seen = False
        for position, indexed_race in enumerate(races, start=1):
            complete = self.repository.has_complete_detail(indexed_race.subsession_id, cust_id)
            detail: IRacingDataRace | None = None
            if complete:
                skipped_existing += 1
                stored = self.repository.race_season(indexed_race.subsession_id, cust_id)
                matches_target = target is None or self._matches_target(target, stored)
            else:
                details_requested += 1
                self.progress(f"Fetching race details: {position} / {len(races)}")
                try:
                    detail = self._fetch_detail(indexed_race, cust_id)
                    if detail is None:
                        failed += 1
                        self.progress(
                            f"[{position}/{len(races)}] missing Race row for "
                            f"{indexed_race.subsession_id}"
                        )
                        continue
                    matches_target = target is None or self._matches_target(
                        target, (detail.season_id, detail.season_name, None, None)
                    )
                except SourceBlockedError:
                    raise
                except Exception as exc:
                    failed += 1
                    self.progress(f"[{position}/{len(races)}] {indexed_race.subsession_id}: {exc}")
                    continue

            if target is not None and not matches_target:
                # iRStats is newest-first. The first older detail after a
                # target detail is the season boundary; if the whole page is
                # older, only the current-season workflow can stop. A
                # historical backfill must keep searching newer pages until
                # it finds the requested season.
                if target_seen or stop_before_target:
                    return details_requested, imported, skipped_existing, failed, True
                continue
            if target is not None:
                target_seen = True
            if complete or detail is None:
                continue
            self.progress("Saving...")
            self.repository.upsert_race(self._normalize(indexed_race, detail))
            imported += 1
            self.repository.set_meta("last_iracingdata_sync", _now())
        return (
            details_requested,
            imported,
            skipped_existing,
            failed,
            target is not None and not target_seen and stop_before_target,
        )

    def _iter_index_pages(
        self,
        cust_id: int,
        *,
        start_page: int,
        max_pages: int | None,
        use_cache: bool,
        force_first_network: bool,
    ) -> Iterator[_PageResult]:
        page_number = start_page
        yielded = 0
        previous_network = False
        fallback_iterator = None
        fetch_page = getattr(self.ir_stats, "fetch_page", None)
        supports_fetch_page = callable(fetch_page)
        while True:
            from_cache = False
            page = None
            if use_cache and not (force_first_network and page_number == start_page):
                page = self.repository.cached_irstats_page(cust_id, page_number)
                from_cache = page is not None
            if page is None:
                if supports_fetch_page:
                    if page_number != start_page and previous_network:
                        self._wait_between_index_requests()
                    self._index_requests += 1
                    page = fetch_page(cust_id, page_number)
                    previous_network = True
                else:
                    if fallback_iterator is None:
                        fallback_iterator = self._history_iterator(
                            cust_id,
                            start_page=start_page,
                            max_pages=max_pages,
                        )
                    self._index_requests += 1
                    page = next(fallback_iterator)
                    previous_network = True
            else:
                previous_network = False
            yield _PageResult(page=page, from_cache=from_cache)
            yielded += 1
            if not page.has_more or (max_pages is not None and yielded >= max_pages):
                return
            page_number = page.next_page or page.page + 1

    def _wait_between_index_requests(self) -> None:
        delay = getattr(self.ir_stats, "page_delay", 0.0)
        sleeper = getattr(self.ir_stats, "sleeper", None)
        if delay and callable(sleeper):
            self.progress(f"Next request in {delay:g} seconds")
            sleeper(delay)

    def _history_iterator(
        self,
        cust_id: int,
        *,
        start_page: int,
        max_pages: int | None,
    ):
        method = self.ir_stats.iter_history
        try:
            parameters = inspect.signature(method).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        kwargs: dict[str, Any] = {}
        if accepts_kwargs or "start_page" in parameters:
            kwargs["start_page"] = start_page
        if accepts_kwargs or "max_pages" in parameters:
            kwargs["max_pages"] = max_pages
        return method(cust_id, **kwargs)

    def _full_rescan_start_page(self, cust_id: int) -> int:
        del cust_id  # Reserved for per-driver metadata if the schema expands.
        incomplete = self.repository.get_meta("index_sync_incomplete", False)
        last_page = _as_int(self.repository.get_meta("last_successful_irstats_page")) or 0
        return last_page + 1 if incomplete else 1

    def _rate_limit_message(
        self,
        error: RateLimitError,
        *,
        completed_pages: int,
        total_pages: int | None,
    ) -> str:
        total = total_pages if total_pages is not None else "?"
        cooldown = f"{error.retry_after if error.retry_after is not None else 60.0:g} seconds"
        return (
            "iRStats rate limit reached.\n\n"
            "Imported progress was saved.\n"
            f"Completed pages: {completed_pages} / {total}\n\n"
            f"Please wait about {cooldown} and press Rescan season or resume the career import."
        )

    def _fetch_detail(self, indexed: IrstatsRaceIndex, cust_id: int) -> IRacingDataRace | None:
        return self.iracing_data.get_race(indexed.subsession_id, cust_id)

    def _reset_driver_checkpoint_if_needed(self, cust_id: int) -> None:
        checkpoint_cust_id = _as_int(self.repository.get_meta("irstats_checkpoint_cust_id"))
        if checkpoint_cust_id not in (None, cust_id):
            self.repository.set_meta("last_successful_irstats_page", 0)
            self.repository.set_meta("total_pages", None)
            self.repository.set_meta("index_sync_incomplete", False)
        self.repository.set_meta("irstats_checkpoint_cust_id", cust_id)

    def _current_season(self) -> SeasonInfo | None:
        method = getattr(self.metadata, "current_season", None)
        if not callable(method):
            return None
        try:
            return method()
        except Exception as exc:
            self.progress(f"Current season metadata unavailable: {exc}")
            return None

    def _target_season(self, season_key: str | None) -> SeasonInfo | None:
        if not season_key:
            return None
        method = getattr(self.metadata, "season_for_key", None)
        if callable(method):
            target = method(season_key)
            if target is not None:
                return target
        try:
            year_text, quarter_text = season_key.split("-", 1)
            return SeasonInfo(int(year_text), int(quarter_text))
        except (AttributeError, ValueError):
            return None

    @staticmethod
    def _matches_target(
        target: SeasonInfo,
        stored: tuple[int | None, str | None, int | None, int | None] | None,
    ) -> bool:
        if stored is None:
            return False
        season_id, season_name, year, quarter = stored
        if season_id is not None and season_id in target.season_ids:
            return True
        identity = (
            (year, quarter)
            if year is not None and quarter is not None
            else season_identity(season_name)
        )
        return identity == (target.year, target.quarter)

    def _refresh_metadata_catalog(self) -> None:
        if getattr(self.metadata, "category_by_id", None) and getattr(
            self.metadata, "season_records", None
        ):
            return
        try:
            self.metadata.refresh_all_seasons()
            for season in getattr(self.metadata, "season_records", []):
                season_id = _as_int(season.get("season_id"))
                season_name = season.get("season_name")
                if season_id is not None and season_name:
                    self.repository.upsert_season_catalog(
                        season_id,
                        season_name,
                        normalize_category(season.get("category")),
                        season,
                    )
        except Exception as exc:
            self.progress(f"Season metadata unavailable; continuing with cached fields: {exc}")

    def _normalize(self, indexed: IrstatsRaceIndex, detail: IRacingDataRace) -> RaceResult:
        category = normalize_category(indexed.category) or self.metadata.category_for(
            detail.season_id, detail.season_name
        )
        schedule = []
        if detail.season_name:
            try:
                schedule = self.metadata.get_schedule(detail.season_name, detail.season_id)
                for entry in schedule:
                    self.repository.upsert_schedule(entry)
            except Exception as exc:
                self.progress(f"Schedule metadata unavailable for {detail.season_name}: {exc}")

        identity = season_identity(detail.season_name)
        year, quarter = identity if identity is not None else (None, None)
        race_week_num, week_source = _resolve_week(detail.start_time_utc, detail.track_name, schedule)
        return RaceResult(
            subsession_id=detail.subsession_id,
            cust_id=detail.cust_id,
            start_time_utc=detail.start_time_utc or indexed.start_time_utc or datetime.now(timezone.utc),
            season_id=detail.season_id,
            season_name=detail.season_name,
            series_name=indexed.series_name,
            category=category,
            track_name=detail.track_name or indexed.track_name,
            car_id=detail.car_id,
            car_name=detail.car_name or indexed.car_name,
            old_irating=detail.old_irating,
            new_irating=detail.new_irating,
            start_position=_display_position(detail.starting_position_api),
            finish_position=_display_position(detail.finish_position_api),
            start_position_api=detail.starting_position_api,
            finish_position_api=detail.finish_position_api,
            incidents=detail.incidents,
            sof=detail.sof,
            season_year=year,
            season_quarter=quarter,
            race_week_num=race_week_num,
            race_week_source=week_source,
            raw_irstats=indexed.raw,
            raw_iracingdata=detail.raw,
        )


def _display_position(value: int | None) -> int | None:
    return value + 1 if value is not None and value >= 0 else None


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _resolve_week(
    start_time: datetime | None,
    track_name: str | None,
    schedule: list[Any],
) -> tuple[int | None, str | None]:
    if not schedule:
        return None, None
    if start_time is not None:
        dated = [entry for entry in schedule if entry.start_date and entry.start_date <= start_time]
        if dated:
            return max(dated, key=lambda entry: entry.start_date).race_week_num, "schedule"
    if track_name:
        normalized = track_name.casefold()
        for entry in schedule:
            if entry.track_name and (
                entry.track_name.casefold() in normalized or normalized in entry.track_name.casefold()
            ):
                return entry.race_week_num, "track-schedule"
    return None, None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
