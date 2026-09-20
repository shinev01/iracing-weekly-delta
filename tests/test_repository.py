from datetime import datetime, timezone

from app.database.repository import RaceRepository
from app.models.normalized import RaceResult
from app.services.irstats import IrstatsPage, IrstatsRaceIndex


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


def test_cached_irstats_page_rehydrates_index_without_network(tmp_path) -> None:
    repo = RaceRepository(tmp_path / "index-cache.db")
    page = IrstatsPage(
        page=2,
        per_page=50,
        has_more=True,
        total_count=120,
        total_pages=3,
        next_page=3,
        races=[
            IrstatsRaceIndex(
                subsession_id=777,
                series_name="Cached Series",
                category="sports_car",
                raw={"subsession_id": 777},
            )
        ],
        raw={"page": 2},
    )
    repo.upsert_irstats_page(456, page)
    repo.upsert_irstats_index(456, page.races[0], page.page)

    cached = repo.cached_irstats_page(456, 2)

    assert cached is not None
    assert cached.page == 2
    assert cached.has_more is True
    assert cached.next_page == 3
    assert cached.races[0].subsession_id == 777
    assert cached.races[0].series_name == "Cached Series"
