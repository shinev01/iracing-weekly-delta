"""Public irstats driver-history index client and HTML parser."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.models.normalized import parse_utc_datetime
from app.services.http import ResilientHTTPClient

_INTEGER = re.compile(r"-?\d+")


@dataclass(slots=True)
class IrstatsRaceIndex:
    """The driver-index fields exposed by irstats."""

    subsession_id: int
    start_time_utc: datetime | None = None
    series_name: str | None = None
    category: str | None = None
    car_name: str | None = None
    track_name: str | None = None
    finish_position: int | None = None
    incidents: int | None = None
    sof: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class IrstatsPage:
    page: int
    per_page: int
    has_more: bool
    races: list[IrstatsRaceIndex]
    raw: dict[str, Any]


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _integer(value: str | None) -> int | None:
    if not value:
        return None
    match = _INTEGER.search(value.replace(",", ""))
    return int(match.group()) if match else None


def _position(value: str | None) -> int | None:
    parsed = _integer(value)
    if parsed is None or parsed < 1:
        return None
    return parsed


def _href_id(href: str | None, prefix: str) -> int | None:
    if not href:
        return None
    path = urlparse(href).path.rstrip("/")
    parts = path.split("/")
    if len(parts) < 2 or parts[-2] != prefix:
        return None
    candidate = parts[-1]
    return int(candidate) if candidate.isdigit() else None


def _field(row: Any, name: str) -> str | None:
    direct = row.get(f"data-{name}")
    if direct:
        return _clean_text(direct)
    element = row.select_one(f"[data-field='{name}']")
    return _clean_text(element.get_text(" ", strip=True)) if element else None


def _link_text(row: Any, prefixes: tuple[str, ...]) -> str | None:
    for link in row.find_all("a", href=True):
        path = urlparse(link["href"]).path
        if any(path.startswith(prefix) for prefix in prefixes):
            return _clean_text(link.get_text(" ", strip=True))
    return None


def parse_irstats_page(payload: dict[str, Any], cust_id: int) -> IrstatsPage:
    """Parse a single irstats JSON page using BeautifulSoup, not HTML regex."""
    html = payload.get("html") or ""
    soup = BeautifulSoup(html, "html.parser")
    races: list[IrstatsRaceIndex] = []
    for row in soup.select("tr"):
        result_link = row.select_one("a[href*='/race/']")
        subsession_id = _href_id(result_link.get("href") if result_link else None, "race")
        if subsession_id is None:
            continue

        time_tag = row.select_one("time[datetime]")
        raw_start = _field(row, "start-time") or _field(row, "date")
        if time_tag:
            raw_start = time_tag.get("datetime") or raw_start

        finish = _field(row, "finish")
        if finish is None:
            finish_cell = row.select_one(".finish, .result, .position")
            finish = _clean_text(finish_cell.get_text(" ", strip=True)) if finish_cell else None

        incidents = _field(row, "incidents")
        if incidents is None:
            incident_cell = row.select_one(".incidents, .inc")
            incidents = _clean_text(incident_cell.get_text(" ", strip=True)) if incident_cell else None

        sof = _field(row, "sof")
        if sof is None:
            sof_cell = row.select_one(".sof, .strength-of-field")
            sof = _clean_text(sof_cell.get_text(" ", strip=True)) if sof_cell else None

        race = IrstatsRaceIndex(
            subsession_id=subsession_id,
            start_time_utc=parse_utc_datetime(raw_start),
            series_name=_field(row, "series") or _link_text(row, ("/series/",)),
            category=_field(row, "category"),
            car_name=_field(row, "car") or _link_text(row, ("/car/",)),
            track_name=_field(row, "track") or _link_text(row, ("/track/",)),
            finish_position=_position(finish),
            incidents=_integer(incidents),
            sof=_integer(sof),
            raw={
                "cust_id": cust_id,
                "subsession_id": subsession_id,
                "row_html": str(row),
                "start_time": raw_start,
                "series": _field(row, "series") or _link_text(row, ("/series/",)),
                "category": _field(row, "category"),
                "car": _field(row, "car") or _link_text(row, ("/car/",)),
                "track": _field(row, "track") or _link_text(row, ("/track/",)),
                "finish": finish,
                "incidents": incidents,
                "sof": sof,
            },
        )
        races.append(race)

    return IrstatsPage(
        page=int(payload.get("page") or 1),
        per_page=int(payload.get("per_page") or 50),
        has_more=bool(payload.get("has_more")),
        races=races,
        raw=payload,
    )


class IrstatsClient:
    """Sequential, paginated irstats client."""

    base_url = "https://irstats.com/api/driver/{cust_id}/races"

    def __init__(
        self,
        http: ResilientHTTPClient | None = None,
        *,
        page_delay: float = 0.75,
        sleeper: Any = time.sleep,
    ) -> None:
        self.http = http or ResilientHTTPClient()
        self.page_delay = page_delay
        self.sleeper = sleeper

    def fetch_page(self, cust_id: int, page: int, per_page: int = 50) -> IrstatsPage:
        url = self.base_url.format(cust_id=cust_id)
        url = f"{url}?page={page}&per_page={min(per_page, 50)}"
        payload, _ = self.http.get_json(url)
        if not isinstance(payload, dict):
            raise ValueError("irstats returned a non-object JSON response")
        return parse_irstats_page(payload, cust_id)

    def iter_history(
        self,
        cust_id: int,
        *,
        start_page: int = 1,
        max_pages: int | None = None,
    ) -> Iterator[IrstatsPage]:
        page_number = start_page
        while True:
            page = self.fetch_page(cust_id, page_number)
            yield page
            if not page.has_more or (max_pages is not None and page_number >= max_pages):
                return
            page_number += 1
            if self.page_delay:
                self.sleeper(self.page_delay)

