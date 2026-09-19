"""Provider-independent race result models."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

CATEGORY_LABELS: dict[str, str] = {
    "sports_car": "Sports Car",
    "formula_car": "Formula Car",
    "oval": "Oval",
    "dirt_road": "Dirt Road",
    "dirt_oval": "Dirt Oval",
}


def normalize_category(value: str | None) -> str | None:
    """Normalize iRacing category values while preserving unknown values."""
    if value is None:
        return None
    normalized = value.strip().lower().replace(" ", "_")
    aliases = {
        "sports": "sports_car",
        "sports_car": "sports_car",
        "sportscar": "sports_car",
        "formula": "formula_car",
        "formula_car": "formula_car",
        "dirtroad": "dirt_road",
        "dirt_road": "dirt_road",
        "dirtoval": "dirt_oval",
        "dirt_oval": "dirt_oval",
    }
    return aliases.get(normalized, normalized)


def category_label(value: str | None) -> str:
    """Return a human-readable category label."""
    if not value:
        return "Unknown"
    return CATEGORY_LABELS.get(value, value.replace("_", " ").title())


def parse_utc_datetime(value: Any) -> datetime | None:
    """Parse ISO/date values into timezone-aware UTC datetimes."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            result = datetime.fromisoformat(text)
        except ValueError:
            try:
                result = datetime.strptime(text, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def isoformat_utc(value: datetime | None) -> str | None:
    """Serialize a datetime in stable UTC ISO format."""
    if value is None:
        return None
    return parse_utc_datetime(value).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class RaceResult:
    """Normalized official Race result.

    ``start_position`` and ``finish_position`` are human-facing one-based
    positions. The raw zero-based API values are retained in the two explicit
    ``*_api`` fields so the adapter boundary is unambiguous.
    """

    subsession_id: int
    cust_id: int
    start_time_utc: datetime

    season_id: int | None = None
    season_name: str | None = None

    series_name: str | None = None
    category: str | None = None
    track_name: str | None = None

    car_id: int | None = None
    car_name: str | None = None

    old_irating: int | None = None
    new_irating: int | None = None

    start_position: int | None = None
    finish_position: int | None = None
    start_position_api: int | None = None
    finish_position_api: int | None = None

    incidents: int | None = None
    sof: int | None = None

    season_year: int | None = None
    season_quarter: int | None = None
    race_week_num: int | None = None
    race_week_source: str | None = None

    raw_irstats: dict[str, Any] | None = None
    raw_iracingdata: dict[str, Any] | None = None

    @property
    def irating_delta(self) -> int | None:
        """Return the exact per-race iRating change."""
        if self.old_irating is None or self.new_irating is None:
            return None
        return self.new_irating - self.old_irating

    @property
    def category_name(self) -> str:
        return category_label(self.category)

    @property
    def display_start_position(self) -> str:
        return f"P{self.start_position}" if self.start_position else "—"

    @property
    def display_finish_position(self) -> str:
        return f"P{self.finish_position}" if self.finish_position else "—"

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["start_time_utc"] = isoformat_utc(self.start_time_utc)
        result["irating_delta"] = self.irating_delta
        result["category_name"] = self.category_name
        result["display_start_position"] = self.display_start_position
        result["display_finish_position"] = self.display_finish_position
        return result

