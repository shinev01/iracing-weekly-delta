from datetime import datetime, timezone

from app.database.repository import RaceRepository
from app.models.normalized import RaceResult


def test_upsert_does_not_duplicate_same_customer_subsession(tmp_path) -> None:
    repo = RaceRepository(tmp_path / "test.db")
    result = RaceResult(
        subsession_id=123,
        cust_id=456,
        start_time_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        category="sports_car",
        old_irating=1000,
        new_irating=1020,
        raw_iracingdata={"results": [{"simsession_name": "RACE"}]},
    )
    repo.upsert_race(result)
    result.new_irating = 1030
    repo.upsert_race(result)

    assert repo.count(456) == 1
    assert repo.list_races(456)[0].new_irating == 1030
    assert repo.has_complete_detail(123, 456) is True
