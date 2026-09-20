"""SQLite repository with idempotent race UPSERTs."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.models.normalized import RaceResult, isoformat_utc, parse_utc_datetime
from app.services.iracingdata import ScheduleEntry, season_identity
from app.services.irstats import IrstatsPage, IrstatsRaceIndex


SCHEMA = """
CREATE TABLE IF NOT EXISTS races (
    subsession_id INTEGER NOT NULL,
    cust_id INTEGER NOT NULL,
    start_time_utc TEXT NOT NULL,
    season_id INTEGER,
    season_name TEXT,
    series_name TEXT,
    category TEXT,
    track_name TEXT,
    car_id INTEGER,
    car_name TEXT,
    old_irating INTEGER,
    new_irating INTEGER,
    start_position INTEGER,
    finish_position INTEGER,
    start_position_api INTEGER,
    finish_position_api INTEGER,
    incidents INTEGER,
    sof INTEGER,
    season_year INTEGER,
    season_quarter INTEGER,
    race_week_num INTEGER,
    race_week_source TEXT,
    raw_irstats TEXT,
    raw_iracingdata TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (subsession_id, cust_id)
);

CREATE INDEX IF NOT EXISTS idx_races_cust_time
    ON races (cust_id, start_time_utc);
CREATE INDEX IF NOT EXISTS idx_races_filter
    ON races (cust_id, category, season_id, series_name, car_name, track_name);

CREATE TABLE IF NOT EXISTS season_schedule (
    season_id INTEGER NOT NULL,
    season_name TEXT NOT NULL,
    category TEXT,
    race_week_num INTEGER NOT NULL,
    week_start_utc TEXT,
    track_name TEXT,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (season_id, race_week_num)
);

CREATE TABLE IF NOT EXISTS season_catalog (
    season_id INTEGER PRIMARY KEY,
    season_name TEXT NOT NULL,
    category TEXT,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sync_metadata (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS irstats_index (
    cust_id INTEGER NOT NULL,
    page INTEGER NOT NULL,
    subsession_id INTEGER NOT NULL,
    start_time_utc TEXT,
    series_name TEXT,
    category TEXT,
    car_name TEXT,
    track_name TEXT,
    finish_position INTEGER,
    incidents INTEGER,
    sof INTEGER,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (cust_id, subsession_id)
);
CREATE INDEX IF NOT EXISTS idx_irstats_index_page
    ON irstats_index (cust_id, page);

CREATE TABLE IF NOT EXISTS irstats_pages (
    cust_id INTEGER NOT NULL,
    page INTEGER NOT NULL,
    per_page INTEGER NOT NULL,
    has_more INTEGER NOT NULL,
    total_count INTEGER,
    total_pages INTEGER,
    next_page INTEGER,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (cust_id, page)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class RaceRepository:
    """Small explicit repository around sqlite3.

    A connection is opened per operation so CLI and FastAPI requests are safe
    without a global connection or thread-local lifecycle complexity.
    """

    def __init__(self, path: str | Path = "iracing.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    def upsert_race(self, race: RaceResult) -> None:
        now = _now()
        values = {
            "subsession_id": race.subsession_id,
            "cust_id": race.cust_id,
            "start_time_utc": isoformat_utc(race.start_time_utc),
            "season_id": race.season_id,
            "season_name": race.season_name,
            "series_name": race.series_name,
            "category": race.category,
            "track_name": race.track_name,
            "car_id": race.car_id,
            "car_name": race.car_name,
            "old_irating": race.old_irating,
            "new_irating": race.new_irating,
            "start_position": race.start_position,
            "finish_position": race.finish_position,
            "start_position_api": race.start_position_api,
            "finish_position_api": race.finish_position_api,
            "incidents": race.incidents,
            "sof": race.sof,
            "season_year": race.season_year,
            "season_quarter": race.season_quarter,
            "race_week_num": race.race_week_num,
            "race_week_source": race.race_week_source,
            "raw_irstats": json.dumps(race.raw_irstats or {}, ensure_ascii=False),
            "raw_iracingdata": json.dumps(race.raw_iracingdata or {}, ensure_ascii=False),
            "created_at": now,
            "updated_at": now,
        }
        columns = ", ".join(values)
        placeholders = ", ".join(f":{key}" for key in values)
        update_columns = ", ".join(
            f"{key}=excluded.{key}"
            for key in values
            if key not in {"subsession_id", "cust_id", "created_at"}
        )
        query = f"""
            INSERT INTO races ({columns}) VALUES ({placeholders})
            ON CONFLICT(subsession_id, cust_id) DO UPDATE SET {update_columns}
        """
        with self.connect() as connection:
            connection.execute(query, values)

    def has_complete_detail(self, subsession_id: int, cust_id: int) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT raw_iracingdata FROM races
                WHERE subsession_id = ? AND cust_id = ?
                """,
                (subsession_id, cust_id),
            ).fetchone()
        if not row or not row["raw_iracingdata"]:
            return False
        try:
            payload = json.loads(row["raw_iracingdata"])
        except json.JSONDecodeError:
            return False
        return isinstance(payload, dict) and bool(payload.get("results"))

    def upsert_irstats_index(self, cust_id: int, race: Any, page: int) -> None:
        """Persist one public index row before its detail request is made."""
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO irstats_index (
                    cust_id, page, subsession_id, start_time_utc, series_name,
                    category, car_name, track_name, finish_position, incidents,
                    sof, raw_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cust_id, subsession_id) DO UPDATE SET
                    page=excluded.page,
                    start_time_utc=excluded.start_time_utc,
                    series_name=excluded.series_name,
                    category=excluded.category,
                    car_name=excluded.car_name,
                    track_name=excluded.track_name,
                    finish_position=excluded.finish_position,
                    incidents=excluded.incidents,
                    sof=excluded.sof,
                    raw_json=excluded.raw_json,
                    updated_at=excluded.updated_at
                """,
                (
                    cust_id,
                    page,
                    race.subsession_id,
                    isoformat_utc(race.start_time_utc),
                    race.series_name,
                    race.category,
                    race.car_name,
                    race.track_name,
                    race.finish_position,
                    race.incidents,
                    race.sof,
                    json.dumps(race.raw or {}, ensure_ascii=False),
                    _now(),
                ),
            )

    def upsert_irstats_page(self, cust_id: int, page: IrstatsPage) -> None:
        """Persist page-level pagination metadata alongside index rows."""
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO irstats_pages(
                    cust_id, page, per_page, has_more, total_count, total_pages,
                    next_page, raw_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cust_id, page) DO UPDATE SET
                    per_page=excluded.per_page,
                    has_more=excluded.has_more,
                    total_count=excluded.total_count,
                    total_pages=excluded.total_pages,
                    next_page=excluded.next_page,
                    raw_json=excluded.raw_json,
                    updated_at=excluded.updated_at
                """,
                (
                    cust_id,
                    page.page,
                    page.per_page,
                    int(page.has_more),
                    page.total_count,
                    page.total_pages,
                    page.next_page,
                    json.dumps(page.raw or {}, ensure_ascii=False),
                    _now(),
                ),
            )

    def cached_irstats_page(self, cust_id: int, page_number: int) -> IrstatsPage | None:
        """Rehydrate one cached index page without contacting iRStats."""
        with self.connect() as connection:
            page_row = connection.execute(
                "SELECT * FROM irstats_pages WHERE cust_id = ? AND page = ?",
                (cust_id, page_number),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT * FROM irstats_index
                WHERE cust_id = ? AND page = ?
                ORDER BY rowid
                """,
                (cust_id, page_number),
            ).fetchall()
        if not rows:
            return None

        races: list[IrstatsRaceIndex] = []
        for row in rows:
            try:
                raw = json.loads(row["raw_json"] or "{}")
            except json.JSONDecodeError:
                raw = {}
            races.append(
                IrstatsRaceIndex(
                    subsession_id=int(row["subsession_id"]),
                    start_time_utc=parse_utc_datetime(row["start_time_utc"]),
                    series_name=row["series_name"],
                    category=row["category"],
                    car_name=row["car_name"],
                    track_name=row["track_name"],
                    finish_position=row["finish_position"],
                    incidents=row["incidents"],
                    sof=row["sof"],
                    raw=raw,
                )
            )

        cached_total = (
            int(page_row["total_pages"])
            if page_row and page_row["total_pages"] is not None
            else self.get_meta("total_pages")
        )
        has_more = (
            bool(page_row["has_more"])
            if page_row
            else bool(cached_total and page_number < int(cached_total))
        )
        total_count = page_row["total_count"] if page_row else None
        next_page = page_row["next_page"] if page_row else (page_number + 1 if has_more else None)
        return IrstatsPage(
            page=page_number,
            per_page=int(page_row["per_page"]) if page_row else len(races),
            has_more=has_more,
            races=races,
            raw=json.loads(page_row["raw_json"] or "{}") if page_row else {"cached": True},
            total_count=total_count,
            total_pages=cached_total,
            next_page=next_page,
        )

    def cached_irstats_pages(self, cust_id: int) -> list[int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT page FROM irstats_index WHERE cust_id = ? ORDER BY page",
                (cust_id,),
            ).fetchall()
        return [int(row["page"]) for row in rows]

    def irstats_index_count(self, cust_id: int) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM irstats_index WHERE cust_id = ?",
                (cust_id,),
            ).fetchone()
        return int(row["count"])

    def existing_subsessions(self, cust_id: int) -> set[int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT subsession_id FROM races WHERE cust_id = ?",
                (cust_id,),
            ).fetchall()
        return {int(row["subsession_id"]) for row in rows}

    def race_season(self, subsession_id: int, cust_id: int) -> tuple[int | None, str | None, int | None, int | None] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT season_id, season_name, season_year, season_quarter
                FROM races WHERE subsession_id = ? AND cust_id = ?
                """,
                (subsession_id, cust_id),
            ).fetchone()
        if not row:
            return None
        return (
            row["season_id"],
            row["season_name"],
            row["season_year"],
            row["season_quarter"],
        )

    def upsert_schedule(self, entry: ScheduleEntry) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO season_schedule (
                    season_id, season_name, category, race_week_num,
                    week_start_utc, track_name, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(season_id, race_week_num) DO UPDATE SET
                    season_name=excluded.season_name,
                    category=excluded.category,
                    week_start_utc=excluded.week_start_utc,
                    track_name=excluded.track_name,
                    raw_json=excluded.raw_json
                """,
                (
                    entry.season_id,
                    entry.season_name,
                    entry.category,
                    entry.race_week_num,
                    isoformat_utc(entry.start_date),
                    entry.track_name,
                    json.dumps(entry.raw, ensure_ascii=False),
                ),
            )

    def upsert_season_catalog(
        self,
        season_id: int,
        season_name: str,
        category: str | None,
        raw: dict[str, Any],
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO season_catalog(season_id, season_name, category, raw_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(season_id) DO UPDATE SET
                    season_name=excluded.season_name,
                    category=excluded.category,
                    raw_json=excluded.raw_json,
                    updated_at=excluded.updated_at
                """,
                (
                    season_id,
                    season_name,
                    category,
                    json.dumps(raw, ensure_ascii=False),
                    _now(),
                ),
            )

    def schedules(self, season_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT * FROM season_schedule
                WHERE season_id = ? ORDER BY race_week_num
                """,
                (season_id,),
            ).fetchall()

    def season_options(self, cust_id: int) -> list[dict[str, Any]]:
        """Return grouped season choices from metadata plus local races."""
        groups: dict[str, dict[str, Any]] = {}
        with self.connect() as connection:
            catalog_rows = connection.execute(
                "SELECT season_id, season_name FROM season_catalog"
            ).fetchall()
            race_rows = connection.execute(
                """
                SELECT season_id, season_name, season_year, season_quarter
                FROM races WHERE cust_id = ?
                """,
                (cust_id,),
            ).fetchall()

        def ensure(year: int, quarter: int) -> dict[str, Any]:
            key = f"{year}-{quarter}"
            return groups.setdefault(
                key,
                {
                    "key": key,
                    "label": f"{year} Season {quarter}",
                    "downloaded": False,
                    "races": 0,
                },
            )

        for row in catalog_rows:
            identity = season_identity(row["season_name"])
            if identity is None:
                continue
            group = ensure(*identity)
            group.setdefault("season_ids", []).append(int(row["season_id"]))

        for row in race_rows:
            identity = (
                (row["season_year"], row["season_quarter"])
                if row["season_year"] is not None and row["season_quarter"] is not None
                else season_identity(row["season_name"])
            )
            if identity is None:
                continue
            group = ensure(*identity)
            group["downloaded"] = True
            group["races"] += 1

        for group in groups.values():
            group["season_ids"] = sorted(set(group.get("season_ids", [])))
        return sorted(
            groups.values(),
            key=lambda item: tuple(int(part) for part in item["key"].split("-")),
            reverse=True,
        )

    def set_setting(self, key: str, value: str | None) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO app_settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: Any) -> None:
        serialized = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO sync_metadata(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, serialized),
            )

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM sync_metadata WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return default
        value = row["value"]
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return value

    def count(self, cust_id: int, **filters: str | int | None) -> int:
        where, params = self._where(cust_id, filters)
        with self.connect() as connection:
            row = connection.execute(f"SELECT COUNT(*) AS count FROM races {where}", params).fetchone()
        return int(row["count"])

    def list_races(
        self,
        cust_id: int,
        *,
        category: str | None = None,
        season_id: int | None = None,
        season_year: int | None = None,
        season_quarter: int | None = None,
        season_name: str | None = None,
        series_name: str | None = None,
        car_name: str | None = None,
        track_name: str | None = None,
    ) -> list[RaceResult]:
        filters = {
            "category": category,
            "season_id": season_id,
            "season_year": season_year,
            "season_quarter": season_quarter,
            "season_name": season_name,
            "series_name": series_name,
            "car_name": car_name,
            "track_name": track_name,
        }
        where, params = self._where(cust_id, filters)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM races {where} ORDER BY start_time_utc ASC, subsession_id ASC",
                params,
            ).fetchall()
        return [self._row_to_race(row) for row in rows]

    def filter_values(self, cust_id: int, field: str) -> list[str]:
        allowed = {"category", "season_name", "series_name", "car_name", "track_name"}
        if field not in allowed:
            raise ValueError(f"Unsupported filter field: {field}")
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT DISTINCT {field} AS value FROM races
                WHERE cust_id = ? AND {field} IS NOT NULL AND {field} != ''
                ORDER BY value
                """,
                (cust_id,),
            ).fetchall()
        return [str(row["value"]) for row in rows]

    def latest_debug(self, cust_id: int) -> dict[str, Any]:
        with self.connect() as connection:
            bounds = connection.execute(
                """
                SELECT COUNT(*) AS count, MIN(start_time_utc) AS oldest,
                       MAX(start_time_utc) AS newest,
                       MAX(subsession_id) AS last_subsession
                FROM races WHERE cust_id = ?
                """,
                (cust_id,),
            ).fetchone()
        return {
            "last_irstats_sync": self.get_meta("last_irstats_sync"),
            "last_irstats_requests": self.get_meta("last_irstats_requests"),
            "last_iracingdata_requests": self.get_meta("last_iracingdata_requests"),
            "last_sync_imported": self.get_meta("last_sync_imported"),
            "last_sync_cached_pages": self.get_meta("last_sync_cached_pages"),
            "current_season_name": self.get_meta("current_season_name"),
            "current_season_ids": self.get_meta("current_season_ids"),
            "last_successful_irstats_page": self.get_meta("last_successful_irstats_page"),
            "total_irstats_pages": self.get_meta("total_pages"),
            "index_sync_incomplete": self.get_meta("index_sync_incomplete", False),
            "cached_irstats_subsessions": self.irstats_index_count(cust_id),
            "sync_progress": self.get_meta("sync_progress"),
            "last_iracingdata_sync": self.get_meta("last_iracingdata_sync"),
            "last_sync": self.get_meta("last_sync"),
            "last_sync_error": self.get_meta("last_sync_error"),
            "number_of_races": int(bounds["count"]),
            "last_subsession": bounds["last_subsession"],
            "oldest_known_race": bounds["oldest"],
            "newest_known_race": bounds["newest"],
        }

    def _where(self, cust_id: int, filters: dict[str, Any]) -> tuple[str, list[Any]]:
        clauses = ["cust_id = ?"]
        params: list[Any] = [cust_id]
        for field, value in filters.items():
            if value not in (None, ""):
                clauses.append(f"{field} = ?")
                params.append(value)
        return "WHERE " + " AND ".join(clauses), params

    @staticmethod
    def _row_to_race(row: sqlite3.Row) -> RaceResult:
        raw_irstats = json.loads(row["raw_irstats"] or "{}")
        raw_iracingdata = json.loads(row["raw_iracingdata"] or "{}")
        return RaceResult(
            subsession_id=row["subsession_id"],
            cust_id=row["cust_id"],
            start_time_utc=parse_utc_datetime(row["start_time_utc"]),
            season_id=row["season_id"],
            season_name=row["season_name"],
            series_name=row["series_name"],
            category=row["category"],
            track_name=row["track_name"],
            car_id=row["car_id"],
            car_name=row["car_name"],
            old_irating=row["old_irating"],
            new_irating=row["new_irating"],
            start_position=row["start_position"],
            finish_position=row["finish_position"],
            start_position_api=row["start_position_api"],
            finish_position_api=row["finish_position_api"],
            incidents=row["incidents"],
            sof=row["sof"],
            season_year=row["season_year"],
            season_quarter=row["season_quarter"],
            race_week_num=row["race_week_num"],
            race_week_source=row["race_week_source"],
            raw_irstats=raw_irstats,
            raw_iracingdata=raw_iracingdata,
        )
