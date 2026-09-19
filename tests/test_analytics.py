from datetime import datetime, timezone

from app.analytics.weeks import dashboard_summary, weekly_summaries
from app.models.normalized import RaceResult


def race(subsession_id: int, old: int, new: int, when: str, finish: int = 5) -> RaceResult:
    return RaceResult(
        subsession_id=subsession_id,
        cust_id=1,
        start_time_utc=datetime.fromisoformat(when).replace(tzinfo=timezone.utc),
        season_id=6444,
        season_name="IMSA - 2026 Season 4",
        category="sports_car",
        track_name="Okayama",
        car_name="Test Car",
        old_irating=old,
        new_irating=new,
        finish_position=finish,
        incidents=2,
        sof=3000,
        race_week_num=0,
    )


def test_weekly_delta_and_sum_match_for_continuous_history() -> None:
    summaries = weekly_summaries([
        race(1, 3000, 3040, "2026-09-16T10:00:00"),
        race(2, 3040, 3010, "2026-09-17T10:00:00", finish=1),
    ])

    assert len(summaries) == 1
    assert summaries[0].delta == 10
    assert summaries[0].sum_race_deltas == 10
    assert summaries[0].continuity_ok is True
    assert summaries[0].wins == 1


def test_weekly_marks_rating_gap_as_incomplete() -> None:
    summaries = weekly_summaries([
        race(1, 3000, 3040, "2026-09-16T10:00:00"),
        race(2, 3090, 3010, "2026-09-17T10:00:00"),
    ])

    assert summaries[0].continuity_ok is False
    assert summaries[0].completeness_label == "Incomplete data"


def test_dashboard_summary_uses_latest_new_rating() -> None:
    summary = dashboard_summary([
        race(1, 3000, 3040, "2026-09-16T10:00:00"),
        race(2, 3040, 3010, "2026-09-17T10:00:00"),
    ])
    assert summary["current_irating"] == 3010
    assert summary["season_change"] == 10

