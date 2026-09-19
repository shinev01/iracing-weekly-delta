"""Public iRacingData result and season metadata clients."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from app.models.normalized import normalize_category, parse_utc_datetime
from app.services.http import ResilientHTTPClient


@dataclass(slots=True)
class IRacingDataRace:
    """The exact Race row for one customer in a subsession."""

    subsession_id: int
    season_id: int | None
    season_name: str | None
    start_time_utc: datetime | None
    track_name: str | None
    sof: int | None
    cust_id: int
    car_id: int | None
    car_name: str | None
    old_irating: int | None
    new_irating: int | None
    starting_position_api: int | None
    finish_position_api: int | None
    incidents: int | None
    raw: dict[str, Any]
    raw_row: dict[str, Any]


@dataclass(slots=True)
class ScheduleEntry:
    season_id: int
    season_name: str
    category: str | None
    race_week_num: int
    start_date: datetime | None
    track_name: str | None
    raw: dict[str, Any]


class IRacingDataClient:
    """Consumer-facing public iRacingData backend client."""

    base_url = "https://iracing6-backend.herokuapp.com/api"

    def __init__(self, http: ResilientHTTPClient | None = None) -> None:
        self.http = http or ResilientHTTPClient()

    def get_detail(self, subsession_id: int) -> dict[str, Any]:
        payload, _ = self.http.get_json(
            f"{self.base_url}/sessionData/results/{subsession_id}"
        )
        if not isinstance(payload, dict):
            raise ValueError("iRacingData returned a non-object result")
        return payload

    def extract_race(self, payload: dict[str, Any], cust_id: int) -> IRacingDataRace | None:
        """Select only this customer from the Race simsession."""
        subsession_id = int(payload.get("subsession_id"))
        for session in payload.get("results", []):
            if str(session.get("simsession_name", "")).upper() != "RACE":
                continue
            for row in session.get("results", []):
                if int(row.get("cust_id", -1)) != cust_id:
                    continue
                return IRacingDataRace(
                    subsession_id=subsession_id,
                    season_id=_as_int(payload.get("season_id")),
                    season_name=payload.get("season_name"),
                    start_time_utc=parse_utc_datetime(payload.get("start_time")),
                    track_name=payload.get("track_name"),
                    sof=_as_int(payload.get("event_strength_of_field")),
                    cust_id=cust_id,
                    car_id=_as_int(row.get("car_id")),
                    car_name=row.get("car_name"),
                    old_irating=_as_int(row.get("oldi_rating")),
                    new_irating=_as_int(row.get("newi_rating")),
                    starting_position_api=_as_int(row.get("starting_position")),
                    finish_position_api=_as_int(row.get("finish_position")),
                    incidents=_as_int(row.get("incidents")),
                    raw=payload,
                    raw_row=row,
                )
        return None

    def get_race(self, subsession_id: int, cust_id: int) -> IRacingDataRace | None:
        return self.extract_race(self.get_detail(subsession_id), cust_id)


class SeasonMetadataClient:
    """Season/category and schedule metadata from the public iRacingData API."""

    def __init__(self, http: ResilientHTTPClient | None = None) -> None:
        self.http = http or ResilientHTTPClient()
        self.category_by_id: dict[int, str] = {}
        self.category_by_name: dict[str, str] = {}
        self.season_records: list[dict[str, Any]] = []
        self.schedules_by_season: dict[int, list[ScheduleEntry]] = {}

    def refresh_all_seasons(self) -> None:
        payload, _ = self.http.get_json(
            "https://iracing6-backend.herokuapp.com/api/series-basic-info/all-seasons"
        )
        seasons = payload.get("seasons", []) if isinstance(payload, dict) else payload
        for season in seasons or []:
            self.season_records.append(season)
            category = normalize_category(season.get("category"))
            season_id = _as_int(season.get("season_id"))
            if season_id is not None and category:
                self.category_by_id[season_id] = category
            season_name = _clean(season.get("season_name"))
            if season_name and category:
                self.category_by_name[season_name.strip()] = category

    def get_schedule(self, season_name: str, season_id: int | None = None) -> list[ScheduleEntry]:
        if season_id is not None and season_id in self.schedules_by_season:
            return self.schedules_by_season[season_id]
        encoded = quote(season_name, safe="")
        payload, _ = self.http.get_json(
            "https://iracing6-backend.herokuapp.com/api/series-basic-info/"
            f"series-basic-info/{encoded}"
        )
        items = payload if isinstance(payload, list) else payload.get("schedule", [])
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            if "schedule" in payload[0]:
                items = payload[0]["schedule"]
        entries: list[ScheduleEntry] = []
        for item in items or []:
            race_week = _as_int(item.get("race_week_num"))
            if race_week is None:
                continue
            track = item.get("track") or {}
            entry = ScheduleEntry(
                season_id=_as_int(item.get("season_id")) or season_id or 0,
                season_name=item.get("season_name") or season_name,
                category=normalize_category(item.get("category")),
                race_week_num=race_week,
                start_date=parse_utc_datetime(item.get("start_date")),
                track_name=track.get("track_name") or item.get("track_name"),
                raw=item,
            )
            entries.append(entry)
        if season_id is not None:
            self.schedules_by_season[season_id] = entries
        return entries

    def category_for(self, season_id: int | None, season_name: str | None) -> str | None:
        if season_id is not None and season_id in self.category_by_id:
            return self.category_by_id[season_id]
        if season_name:
            return self.category_by_name.get(season_name.strip())
        return None


def _clean(value: Any) -> str | None:
    return str(value).strip() if value not in (None, "") else None


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
