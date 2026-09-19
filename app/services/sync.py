"""Resumable full and quick synchronization orchestration."""

from __future__ import annotations

import inspect
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from app.database.repository import RaceRepository
from app.models.normalized import RaceResult, normalize_category
from app.services.http import RateLimitError, SourceBlockedError
from app.services.iracingdata import IRacingDataClient, IRacingDataRace, SeasonMetadataClient
from app.services.irstats import IrstatsClient, IrstatsRaceIndex


class ProgressReporter(Protocol):
    def __call__(self, message: str) -> None: ...


def console_progress(message: str) -> None:
    print(message, flush=True)


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


@dataclass(slots=True)
class _IndexCollection:
    races: list[IrstatsRaceIndex]
    pages: int
    stopped_reason: str | None
    completed_pages: int
    total_pages: int | None


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
        self.detail_workers = max(1, min(detail_workers, 4))
        self.progress = progress

    def sync(self, cust_id: int, *, full_rescan: bool = False) -> SyncReport:
        mode = "full" if full_rescan else "quick"
        self.repository.set_setting("cust_id", str(cust_id))
        checkpoint_cust_id = _as_int(self.repository.get_meta("irstats_checkpoint_cust_id"))
        if checkpoint_cust_id not in (None, cust_id):
            self.repository.set_meta("last_successful_irstats_page", 0)
            self.repository.set_meta("total_pages", None)
            self.repository.set_meta("index_sync_incomplete", False)
        self.repository.set_meta("irstats_checkpoint_cust_id", cust_id)
        self.repository.set_meta("last_sync_error", None)
        existing = self.repository.existing_subsessions(cust_id)
        self.progress(f"Customer ID: {cust_id}")
        self.progress("\nFetching irstats history...")

        collection = self._collect_index(cust_id, full_rescan=full_rescan)
        index = collection.races
        pages = collection.pages
        stopped_reason = collection.stopped_reason
        if stopped_reason:
            self.repository.set_meta("last_sync_error", stopped_reason)
            self.progress(stopped_reason)

        unique_index = {race.subsession_id: race for race in index}
        index = list(unique_index.values())
        self.progress(f"Found: {len(index)} subsessions")
        self.progress(f"Existing locally: {len(existing)}")
        pending = [
            race
            for race in index
            if full_rescan or not self.repository.has_complete_detail(race.subsession_id, cust_id)
        ]
        self.progress(f"Need details: {len(pending)}")
        self.progress("\nFetching iRacingData:")
        imported = 0
        failed = 0
        self._refresh_metadata_catalog()

        futures: dict[Future[IRacingDataRace | None], IrstatsRaceIndex] = {}
        with ThreadPoolExecutor(max_workers=self.detail_workers) as executor:
            for indexed_race in pending:
                futures[executor.submit(self._fetch_detail, indexed_race, cust_id)] = indexed_race

            for position, future in enumerate(as_completed(futures), start=1):
                indexed_race = futures[future]
                try:
                    detail = future.result()
                    if detail is None:
                        failed += 1
                        self.progress(
                            f"[{position}/{len(pending)}] missing Race row for "
                            f"{indexed_race.subsession_id}"
                        )
                        continue
                    normalized = self._normalize(indexed_race, detail)
                    self.repository.upsert_race(normalized)
                    imported += 1
                    self.repository.set_meta("last_iracingdata_sync", _now())
                    self.progress(_progress_line(position, len(pending), normalized))
                except SourceBlockedError as exc:
                    stopped_reason = str(exc)
                    self.repository.set_meta("last_sync_error", stopped_reason)
                    self.progress(stopped_reason)
                    for other in futures:
                        other.cancel()
                    break
                except Exception as exc:  # one bad subsession must not erase prior progress
                    failed += 1
                    self.progress(f"[{position}/{len(pending)}] {indexed_race.subsession_id}: {exc}")

        now = _now()
        self.repository.set_meta("last_irstats_sync", now)
        self.repository.set_meta("last_sync", now)
        self.repository.set_meta("last_sync_mode", mode)
        if stopped_reason:
            self.progress("\nImport stopped; imported progress was saved.")
        else:
            self.progress("\nImport complete.")
        self.progress(f"Races: {self.repository.count(cust_id)}")
        return SyncReport(
            cust_id=cust_id,
            mode=mode,
            pages=pages,
            found_subsessions=len(index),
            existing_locally=len(existing),
            details_requested=len(pending),
            imported=imported,
            skipped_existing=len(index) - len(pending),
            failed=failed,
            stopped_reason=stopped_reason,
            completed_pages=collection.completed_pages,
            total_pages=collection.total_pages,
        )

    def _collect_index(self, cust_id: int, *, full_rescan: bool) -> _IndexCollection:
        races: list[IrstatsRaceIndex] = []
        pages = 0
        stopped_reason: str | None = None
        last_page = None
        start_page = self._full_rescan_start_page(cust_id) if full_rescan else 1
        total_pages = _as_int(self.repository.get_meta("total_pages"))
        self.progress("Reading irstats history...")

        try:
            iterator = self._history_iterator(
                cust_id,
                start_page=start_page,
                max_pages=None if full_rescan else 1,
            )
            for page in iterator:
                pages += 1
                last_page = page
                if page.total_pages is not None:
                    total_pages = page.total_pages
                    self.repository.set_meta("total_pages", total_pages)
                if full_rescan:
                    self.repository.set_meta("last_successful_irstats_page", page.page)
                    self.repository.set_meta("index_sync_incomplete", True)

                # The index is durable before any iRacingData detail requests
                # begin. A later 429 therefore cannot discard earlier pages.
                for race in page.races:
                    upsert_index = getattr(self.repository, "upsert_irstats_index", None)
                    if callable(upsert_index):
                        upsert_index(cust_id, race, page.page)
                races.extend(page.races)
                discovered = len({race.subsession_id for race in races})
                total_label = total_pages if total_pages is not None else "?"
                self.progress(f"Page {page.page} / {total_label}")
                self.progress(f"{discovered} races discovered")
                # Quick Sync is intentionally one request. This also keeps
                # compatibility with simple test doubles that expose the old
                # iter_history(cust_id) signature.
                if not full_rescan:
                    break
        except RateLimitError as exc:
            stopped_reason = self._rate_limit_message(exc, full_rescan=full_rescan)
            if full_rescan:
                self.repository.set_meta("index_sync_incomplete", True)
        except SourceBlockedError as exc:
            stopped_reason = str(exc)
            if full_rescan:
                self.repository.set_meta("index_sync_incomplete", True)

        if full_rescan and stopped_reason is None and last_page is not None and not last_page.has_more:
            self.repository.set_meta("index_sync_incomplete", False)
            self.repository.set_meta("last_successful_irstats_page", last_page.page)
            if total_pages is not None:
                self.repository.set_meta("total_pages", total_pages)

        completed_pages = _as_int(self.repository.get_meta("last_successful_irstats_page")) or 0
        return _IndexCollection(races, pages, stopped_reason, completed_pages, total_pages)

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

    def _rate_limit_message(self, error: RateLimitError, *, full_rescan: bool) -> str:
        completed = (
            _as_int(self.repository.get_meta("last_successful_irstats_page")) or 0
            if full_rescan
            else 0
        )
        total = _as_int(self.repository.get_meta("total_pages"))
        if total is None:
            total = "?"
        cooldown = f"{error.retry_after:g} seconds"
        return (
            "iRStats rate limit reached.\n\n"
            "Imported progress was saved.\n"
            f"Completed pages: {completed} / {total}\n\n"
            f"Please wait about {cooldown} and press Resume import."
        )

    def _fetch_detail(self, indexed: IrstatsRaceIndex, cust_id: int) -> IRacingDataRace | None:
        return self.iracing_data.get_race(indexed.subsession_id, cust_id)

    def _refresh_metadata_catalog(self) -> None:
        if self.metadata.category_by_id:
            return
        try:
            self.metadata.refresh_all_seasons()
            for season in self.metadata.season_records:
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

        year, quarter = _season_identity(detail.season_name)
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


def _season_identity(season_name: str | None) -> tuple[int | None, int | None]:
    if not season_name:
        return None, None
    words = season_name.replace("-", " ").split()
    year = next(
        (int(word) for word in words if word.isdigit() and len(word) == 4),
        None,
    )
    quarter = None
    for index, word in enumerate(words[:-1]):
        if word.lower() == "season" and words[index + 1].isdigit():
            candidate = int(words[index + 1])
            if 1 <= candidate <= 4:
                quarter = candidate
    return year, quarter


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


def _progress_line(position: int, total: int, race: RaceResult) -> str:
    percent = (position / total * 100) if total else 100.0
    delta = race.irating_delta
    delta_text = f"{delta:+d}" if delta is not None else "n/a"
    return (
        f"[{position}/{total}] {percent:.1f}% "
        f"{race.start_time_utc.date()} | {race.series_name or race.season_name or 'Unknown'} | "
        f"{race.track_name or 'Unknown track'} | {race.car_name or 'Unknown car'} | "
        f"{race.old_irating} -> {race.new_irating} ({delta_text})"
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
