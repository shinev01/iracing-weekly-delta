"""iRacing race-week grouping, continuity checks, and dashboard summaries."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from app.models.normalized import RaceResult, category_label


@dataclass(slots=True)
class WeeklySummary:
    season_id: int | None
    season_name: str | None
    category: str | None
    category_name: str
    season_year: int | None
    season_quarter: int | None
    race_week_num: int | None
    race_week_label: str
    start_irating: int | None
    end_irating: int | None
    delta: int | None
    sum_race_deltas: int | None
    race_count: int
    wins: int
    podiums: int
    incidents: int
    avg_sof: int | None
    continuity_ok: bool
    completeness_label: str
    races: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def season_identity(season_name: str | None) -> tuple[int | None, int | None]:
    """Extract year/quarter from iRacing's season name without using date weeks."""
    if not season_name:
        return None, None
    words = season_name.replace("-", " ").split()
    year: int | None = None
    quarter: int | None = None
    for index, word in enumerate(words):
        if word.isdigit() and len(word) == 4 and 2000 <= int(word) <= 2100:
            year = int(word)
        if word.lower() == "season" and index + 1 < len(words):
            candidate = words[index + 1]
            if candidate.isdigit() and 1 <= int(candidate) <= 4:
                quarter = int(candidate)
    return year, quarter


def apply_season_identity(race: RaceResult) -> RaceResult:
    """Fill missing season year/quarter fields in-place and return the race."""
    year, quarter = season_identity(race.season_name)
    race.season_year = race.season_year or year
    race.season_quarter = race.season_quarter or quarter
    return race


def weekly_summaries(races: list[RaceResult]) -> list[WeeklySummary]:
    """Group races by season/category/race-week and mark continuity gaps."""
    for race in races:
        apply_season_identity(race)
    ordered = sorted(races, key=lambda item: (item.start_time_utc, item.subsession_id))
    continuity_by_id: dict[int, bool] = {race.subsession_id: True for race in ordered}
    previous_by_category: dict[str | None, RaceResult] = {}
    for race in ordered:
        previous = previous_by_category.get(race.category)
        if previous is not None:
            if (
                previous.new_irating is None
                or race.old_irating is None
                or previous.new_irating != race.old_irating
            ):
                continuity_by_id[previous.subsession_id] = False
                continuity_by_id[race.subsession_id] = False
        if race.old_irating is None or race.new_irating is None:
            continuity_by_id[race.subsession_id] = False
        previous_by_category[race.category] = race

    grouped: dict[tuple[Any, ...], list[RaceResult]] = {}
    for race in ordered:
        key = (
            race.category,
            race.season_id,
            race.season_year,
            race.season_quarter,
            race.race_week_num,
        )
        grouped.setdefault(key, []).append(race)

    summaries: list[WeeklySummary] = []
    for key, group in grouped.items():
        category, season_id, year, quarter, week_num = key
        group.sort(key=lambda item: (item.start_time_utc, item.subsession_id))
        first, last = group[0], group[-1]
        race_deltas = [race.irating_delta for race in group]
        valid_deltas = [delta for delta in race_deltas if delta is not None]
        weekly_delta = (
            last.new_irating - first.old_irating
            if first.old_irating is not None and last.new_irating is not None
            else None
        )
        sum_deltas = sum(valid_deltas) if len(valid_deltas) == len(group) else None
        continuity = all(continuity_by_id[race.subsession_id] for race in group)
        if weekly_delta is None or sum_deltas is None or weekly_delta != sum_deltas:
            continuity = False
        label = _week_label(year, quarter, week_num)
        summaries.append(
            WeeklySummary(
                season_id=season_id,
                season_name=first.season_name,
                category=category,
                category_name=category_label(category),
                season_year=year,
                season_quarter=quarter,
                race_week_num=week_num,
                race_week_label=label,
                start_irating=first.old_irating,
                end_irating=last.new_irating,
                delta=weekly_delta,
                sum_race_deltas=sum_deltas,
                race_count=len(group),
                wins=sum(1 for race in group if race.finish_position == 1),
                podiums=sum(
                    1 for race in group if race.finish_position is not None and race.finish_position <= 3
                ),
                incidents=sum(race.incidents or 0 for race in group),
                avg_sof=round(sum(race.sof for race in group if race.sof is not None) / len([race for race in group if race.sof is not None]))
                if any(race.sof is not None for race in group)
                else None,
                continuity_ok=continuity,
                completeness_label="Verified" if continuity else "Incomplete data",
                races=[race.to_dict() for race in group],
            )
        )

    return sorted(
        summaries,
        key=lambda item: (
            item.season_year or 0,
            item.season_quarter or 0,
            item.race_week_num if item.race_week_num is not None else 999,
        ),
    )


def every_race_points(races: list[RaceResult]) -> list[dict[str, Any]]:
    """Return one iRating chart point per official Race."""
    points: list[dict[str, Any]] = []
    for race in sorted(races, key=lambda item: (item.start_time_utc, item.subsession_id)):
        if race.new_irating is None:
            continue
        points.append(
            {
                "x": race.start_time_utc.isoformat(),
                "y": race.new_irating,
                "label": race.start_time_utc.strftime("%Y-%m-%d"),
                "subsession_id": race.subsession_id,
                "delta": race.irating_delta,
            }
        )
    return points


def dashboard_summary(races: list[RaceResult]) -> dict[str, Any]:
    """Calculate the dashboard cards for an already-filtered race list."""
    ordered = sorted(races, key=lambda item: (item.start_time_utc, item.subsession_id))
    valid_sof = [race.sof for race in ordered if race.sof is not None]
    if not ordered:
        return {
            "current_irating": None,
            "season_change": None,
            "races": 0,
            "wins": 0,
            "podiums": 0,
            "avg_sof": None,
        }
    first_old = next((race.old_irating for race in ordered if race.old_irating is not None), None)
    last_new = next((race.new_irating for race in reversed(ordered) if race.new_irating is not None), None)
    return {
        "current_irating": last_new,
        "season_change": last_new - first_old if first_old is not None and last_new is not None else None,
        "races": len(ordered),
        "wins": sum(1 for race in ordered if race.finish_position == 1),
        "podiums": sum(
            1 for race in ordered if race.finish_position is not None and race.finish_position <= 3
        ),
        "avg_sof": round(sum(valid_sof) / len(valid_sof)) if valid_sof else None,
    }


def _week_label(year: int | None, quarter: int | None, week_num: int | None) -> str:
    season = f"{year} Season {quarter}" if year and quarter else "Unknown season"
    week = f"Week {week_num + 1}" if week_num is not None else "Week unknown"
    return f"{season} — {week}"

