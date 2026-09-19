"""Resumable full and quick synchronization orchestration."""

from __future__ import annotations

import json
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from app.database.repository import RaceRepository
from app.models.normalized import RaceResult, normalize_category
from app.services.http import SourceBlockedError
from app.services.iracingdata import IRacingDataClient, IRacingDataRace, SeasonMetadataClient
from app.services.irstats import IrstatsClient, IrstatsPage, IrstatsRaceIndex


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
        self.ir_stats = ir_stats or IrstatsClient()
        self.iracing_data = iracing_data or IRacingDataClient()
        self.metadata = metadata or SeasonMetadataClient()
        self.detail_workers = max(1, min(detail_workers, 4))
        self.progress = progress

    def sync(self, cust_id: int, *, full_rescan: bool = False) -> SyncReport:
        mode = "full" if full_rescan else "quick"
        self.repository.set_setting("cust_id", str(cust_id))
        self.repository.set_meta("last_sync_error", None)
        existing = self.repository.existing_subsessions(cust_id)
        self.progress(f"Customer ID: {cust_id}")
        self.progress("\nFetching irstats history...")

        try:
            index, pages = self._collect_index(cust_id, full_rescan=full_rescan)
        except SourceBlockedError as exc:
            self.repository.set_meta("last_sync_error", str(exc))
            self.progress(str(exc))
            return SyncReport(cust_id, mode, 0, 0, len(existing), 0, 0, 0, 0, str(exc))

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
        stopped_reason: str | None = None
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
        )

    def _collect_index(self, cust_id: int, *, full_rescan: bool) -> tuple[list[IrstatsRaceIndex], int]:
        races: list[IrstatsRaceIndex] = []
        pages = 0
        for page in self.ir_stats.iter_history(cust_id):
            pages += 1
            races.extend(page.races)
            self.progress(f"Page {page.page}: {len(page.races)} races")
            if not full_rescan and page.page > 1:
                if all(
                    self.repository.has_complete_detail(race.subsession_id, cust_id)
                    for race in page.races
                ):
                    break
        return races, pages

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
        if race_week_num is None or category is None:
            race_page_fetcher = getattr(self.ir_stats, "fetch_race_page", None)
            if callable(race_page_fetcher):
                try:
                    race_page = race_page_fetcher(detail.subsession_id)
                    if race_week_num is None and race_page.race_week_num is not None:
                        race_week_num = race_page.race_week_num
                        week_source = "irstats-race-page"
                    if category is None and race_page.category:
                        category = race_page.category
                except Exception as exc:
                    self.progress(
                        f"irstats race-page fallback unavailable for "
                        f"{detail.subsession_id}: {exc}"
                    )
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
