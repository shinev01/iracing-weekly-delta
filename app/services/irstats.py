"""Public irstats driver-history HTML client and parser."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Protocol
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from app.models.normalized import normalize_category, parse_utc_datetime
from app.services.http import (
    HTTPResponse,
    RemoteResponseError,
    RemoteSourceError,
    ResilientHTTPClient,
    SourceBlockedError,
)

_INTEGER = re.compile(r"-?\d+")
_SHOWING = re.compile(
    r"Showing\s+([\d,]+)\s*[\u2013\-]\s*([\d,]+)\s+of\s+([\d,]+)\s+races",
    re.IGNORECASE,
)
_WEEK = re.compile(r"\bWeek\s*[:#]?\s*(\d{1,2})\b", re.IGNORECASE)
_CLOUDFLARE_MARKERS = (
    "performing security verification",
    "checking your browser",
    "cf-chl-",
    "challenge-platform",
)


@dataclass(slots=True)
class IrstatsRaceIndex:
    """The driver-index fields exposed by an irstats history row."""

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
    total_count: int | None = None
    total_pages: int | None = None
    next_page: int | None = None


@dataclass(slots=True)
class IrstatsRacePage:
    """Small fallback subset from a public irstats race page."""

    race_week_num: int | None = None
    category: str | None = None


class BrowserHTMLFetcher(Protocol):
    def get_html(self, url: str) -> HTTPResponse: ...


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
    element = row.select_one(f"[data-field='{name}'], .{name}, [class*='{name}']")
    return _clean_text(element.get_text(" ", strip=True)) if element else None


def _link_text(row: Any, prefixes: tuple[str, ...]) -> str | None:
    for link in row.find_all("a", href=True):
        path = urlparse(link["href"]).path
        if any(path.startswith(prefix) for prefix in prefixes):
            return _clean_text(link.get_text(" ", strip=True))
    return None


def _normalize_label(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()


def _header_names(table: Any) -> list[str]:
    header = table.select_one("thead tr")
    if not header:
        return []
    return [
        _normalize_label(cell.get_text(" ", strip=True))
        for cell in header.find_all(["th", "td"], recursive=False)
    ]


def _cell_by_headers(row: Any, headers: list[str], names: tuple[str, ...]) -> str | None:
    cells = row.find_all("td", recursive=False)
    if not cells or not headers:
        return None
    for index, header in enumerate(headers):
        if index < len(cells) and any(name in header for name in names):
            return _clean_text(cells[index].get_text(" ", strip=True))
    return None


def _row_value(
    row: Any,
    headers: list[str],
    field_name: str,
    header_names: tuple[str, ...],
) -> str | None:
    return _field(row, field_name) or _cell_by_headers(row, headers, header_names)


def _parse_start_time(value: str | None) -> datetime | None:
    parsed = parse_utc_datetime(value)
    if parsed is not None or not value:
        return parsed
    for format_value in (
        "%b %d, %Y %H:%M",
        "%b %d, %Y",
        "%B %d, %Y %H:%M",
        "%B %d, %Y",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y",
    ):
        try:
            return datetime.strptime(value, format_value).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _next_page(soup: BeautifulSoup) -> int | None:
    candidates = soup.select("a[rel~='next'], a[aria-label*='Next' i], a.next")
    candidates.extend(
        link
        for link in soup.find_all("a", href=True)
        if _clean_text(link.get_text(" ", strip=True))
        and _clean_text(link.get_text(" ", strip=True)).casefold()
        in {"next", "next page", "›", "→"}
    )
    for link in candidates:
        page = parse_qs(urlparse(link.get("href", "")).query).get("page", [])
        if page and page[0].isdigit():
            return int(page[0])
    return None


def parse_irstats_page(
    html: str,
    cust_id: int,
    page_number: int = 1,
    *,
    per_page: int = 50,
) -> IrstatsPage:
    """Parse one server-rendered irstats history page.

    The history endpoint is HTML-only. ``subsession_id`` is deliberately read
    from the public ``/race/{id}`` link; exact result fields remain an
    iRacingData responsibility.
    """
    if not isinstance(html, str):
        raise TypeError("irstats history must be an HTML string")
    soup = BeautifulSoup(html, "html.parser")
    summary = _SHOWING.search(soup.get_text(" ", strip=True))
    total_count = int(summary.group(3).replace(",", "")) if summary else None
    actual_per_page = per_page
    total_pages = math.ceil(total_count / actual_per_page) if total_count is not None else None
    next_page = _next_page(soup)
    races: list[IrstatsRaceIndex] = []

    for table in soup.select("table"):
        headers = _header_names(table)
        rows = table.select("tbody tr") or table.select("tr")
        for row in rows:
            result_link = row.select_one("a[href*='/race/']")
            subsession_id = _href_id(
                result_link.get("href") if result_link else None, "race"
            )
            if subsession_id is None:
                continue

            time_tag = row.select_one("time[datetime]")
            raw_start = _field(row, "start-time") or _field(row, "date")
            raw_start = raw_start or _cell_by_headers(
                row, headers, ("date", "start time", "started", "time")
            )
            if time_tag:
                raw_start = time_tag.get("datetime") or raw_start

            series = (
                _field(row, "series")
                or _link_text(row, ("/series/",))
                or _cell_by_headers(row, headers, ("series",))
            )
            car = (
                _field(row, "car")
                or _link_text(row, ("/car/", "/cars/"))
                or _cell_by_headers(row, headers, ("car",))
            )
            track = (
                _field(row, "track")
                or _link_text(row, ("/track/", "/tracks/"))
                or _cell_by_headers(row, headers, ("track",))
            )
            finish = _row_value(
                row, headers, "finish", ("finish", "position", "result")
            )
            category = _row_value(row, headers, "category", ("category",))
            incidents = _row_value(row, headers, "incidents", ("incident", "inc"))
            sof = _row_value(row, headers, "sof", ("strength of field", "sof"))
            races.append(
                IrstatsRaceIndex(
                    subsession_id=subsession_id,
                    start_time_utc=_parse_start_time(raw_start),
                    series_name=series,
                    category=normalize_category(category),
                    car_name=car,
                    track_name=track,
                    finish_position=_position(finish),
                    incidents=_integer(incidents),
                    sof=_integer(sof),
                    raw={
                        "cust_id": cust_id,
                        "subsession_id": subsession_id,
                        "row_html": str(row),
                        "start_time": raw_start,
                        "series": series,
                        "category": category,
                        "car": car,
                        "track": track,
                        "finish": finish,
                        "incidents": incidents,
                        "sof": sof,
                    },
                )
            )

    has_more = bool(next_page) or (total_pages is not None and page_number < total_pages)
    return IrstatsPage(
        page=page_number,
        per_page=actual_per_page,
        has_more=has_more,
        races=races,
        raw={
            "cust_id": cust_id,
            "page": page_number,
            "total_count": total_count,
            "total_pages": total_pages,
            "next_page": next_page,
        },
        total_count=total_count,
        total_pages=total_pages,
        next_page=next_page,
    )


def parse_irstats_race_page(html: str) -> IrstatsRacePage:
    """Extract only optional week/category fallback fields from a race page."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    week_match = _WEEK.search(text)
    display_week = int(week_match.group(1)) if week_match else None
    category = None
    for label in soup.find_all(string=re.compile(r"^\s*Category\s*$", re.IGNORECASE)):
        value = label.parent.find_next_sibling() if label.parent else None
        category = _clean_text(value.get_text(" ", strip=True)) if value else None
        if category:
            break
    if category is None:
        category_match = re.search(
            r"Category\s*[:\-]?\s*(Sports Car|Formula Car|Oval|Dirt Road|Dirt Oval|Formula)",
            text,
            re.IGNORECASE,
        )
        category = _clean_text(category_match.group(1)) if category_match else None
    return IrstatsRacePage(
        race_week_num=display_week - 1 if display_week and display_week >= 1 else None,
        category=normalize_category(category),
    )


def _looks_like_cloudflare(body: bytes) -> bool:
    text = body.decode("utf-8", errors="ignore").casefold()
    return any(marker in text for marker in _CLOUDFLARE_MARKERS)


class PersistentBrowserHTMLClient:
    """Visible, ordinary persistent Playwright browser for public irstats HTML."""

    def __init__(
        self,
        *,
        profile_dir: str | Path | None = None,
        channel: str | None = "chrome",
        wait_seconds: float = 180.0,
    ) -> None:
        self.profile_dir = Path(
            profile_dir or Path.home() / ".iracing-weekly-tracker" / "irstats-browser-profile"
        )
        self.channel = channel
        self.wait_seconds = wait_seconds
        self._playwright: Any = None
        self._context: Any = None

    def get_html(self, url: str) -> HTTPResponse:
        parsed = urlparse(url)
        if parsed.hostname not in {"irstats.com", "www.irstats.com"}:
            raise ValueError("The irstats browser fallback only accepts irstats.com URLs")
        if not parsed.path.startswith(("/driver/", "/race/")):
            raise ValueError("The irstats browser fallback only reads public driver/race pages")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RemoteSourceError(
                "Direct irstats HTML is blocked and Playwright is not installed. "
                "Install requirements, then run `playwright install chromium`."
            ) from exc

        if self._context is None:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            self._playwright = sync_playwright().start()
            options: dict[str, Any] = {"headless": False}
            if self.channel:
                options["channel"] = self.channel
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir), **options
            )
        page = self._context.pages[0] if self._context.pages else self._context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        deadline = time.monotonic() + self.wait_seconds
        body = page.content().encode("utf-8")
        while _looks_like_cloudflare(body) and time.monotonic() < deadline:
            page.wait_for_timeout(1_000)
            body = page.content().encode("utf-8")
        if _looks_like_cloudflare(body):
            raise SourceBlockedError(
                "irstats remains behind a Cloudflare browser check. "
                "Complete the ordinary check in the visible persistent browser and retry; "
                "no anti-bot bypass is used."
            )
        if "too many requests" in body.decode("utf-8", errors="ignore").casefold():
            raise SourceBlockedError("irstats browser returned Too Many Requests; retry later.")
        return HTTPResponse(200, {"content-type": "text/html"}, body)

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None


class IrstatsClient:
    """Sequential, paginated irstats HTML client."""

    base_url = "https://irstats.com/driver/{cust_id}/races"
    race_url = "https://irstats.com/race/{subsession_id}"

    def __init__(
        self,
        http: ResilientHTTPClient | None = None,
        *,
        browser: BrowserHTMLFetcher | None = None,
        browser_profile: str | Path | None = None,
        browser_channel: str | None = "chrome",
        browser_wait_seconds: float = 180.0,
        page_delay: float = 0.75,
        sleeper: Any = time.sleep,
    ) -> None:
        self.http = http or ResilientHTTPClient()
        self.browser = browser if browser is not None else PersistentBrowserHTMLClient(
            profile_dir=browser_profile,
            channel=browser_channel,
            wait_seconds=browser_wait_seconds,
        )
        self.page_delay = page_delay
        self.sleeper = sleeper

    def _get_html(self, url: str) -> str:
        try:
            response = self.http.get_html(url)
            if _looks_like_cloudflare(response.body):
                raise SourceBlockedError(f"{url} returned a Cloudflare browser check")
            return response.body.decode("utf-8")
        except SourceBlockedError as direct_error:
            if self.browser is None:
                raise
            try:
                response = self.browser.get_html(url)
            except SourceBlockedError:
                raise
            except Exception as exc:
                raise SourceBlockedError(
                    f"{url} was blocked by direct HTML and browser fallback was unavailable: {exc}"
                ) from direct_error
            if response.status < 200 or response.status >= 300:
                raise RemoteResponseError(f"{url} returned HTTP {response.status} in browser.")
            if _looks_like_cloudflare(response.body):
                raise SourceBlockedError(f"{url} remains behind a Cloudflare browser check")
            return response.body.decode("utf-8")

    def fetch_page(self, cust_id: int, page: int, per_page: int = 50) -> IrstatsPage:
        url = f"{self.base_url.format(cust_id=cust_id)}?page={page}"
        html = self._get_html(url)
        return parse_irstats_page(html, cust_id, page, per_page=min(per_page, 50))

    def fetch_race_page(self, subsession_id: int) -> IrstatsRacePage:
        html = self._get_html(self.race_url.format(subsession_id=subsession_id))
        return parse_irstats_race_page(html)

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
