from datetime import datetime, timezone

from app.database.repository import RaceRepository
from app.services.iracingdata import IRacingDataRace
from app.services.irstats import IrstatsPage, IrstatsRaceIndex
from app.services.sync import SyncService


class FakeIrstats:
    def iter_history(self, cust_id):
        yield IrstatsPage(
            page=1,
            per_page=50,
            has_more=False,
            races=[
                IrstatsRaceIndex(
                    subsession_id=99,
                    series_name="Test Series",
                    category="sports_car",
                    raw={"subsession_id": 99},
                )
            ],
            raw={},
        )


class FakeData:
    def __init__(self):
        self.calls = 0

    def get_race(self, subsession_id, cust_id):
        self.calls += 1
        return IRacingDataRace(
            subsession_id=subsession_id,
            season_id=1,
            season_name="Test Series - 2026 Season 4",
            start_time_utc=datetime(2026, 9, 1, tzinfo=timezone.utc),
            track_name="Test Track",
            sof=2000,
            cust_id=cust_id,
            car_id=2,
            car_name="Test Car",
            old_irating=1000,
            new_irating=1010,
            starting_position_api=0,
            finish_position_api=0,
            incidents=0,
            raw={"results": [{"simsession_name": "RACE"}]},
            raw_row={"cust_id": cust_id},
        )


class FakeMetadata:
    category_by_id = {1: "sports_car"}

    def refresh_all_seasons(self):
        return None

    def category_for(self, season_id, season_name):
        return "sports_car"

    def get_schedule(self, season_name, season_id=None):
        return []


def test_sync_is_resumable_and_skips_complete_details(tmp_path) -> None:
    repo = RaceRepository(tmp_path / "sync.db")
    data = FakeData()
    service = SyncService(
        repo,
        ir_stats=FakeIrstats(),
        iracing_data=data,
        metadata=FakeMetadata(),
        progress=lambda _: None,
    )

    first = service.sync(456)
    second = service.sync(456)

    assert first.imported == 1
    assert second.details_requested == 0
    assert data.calls == 1
    assert repo.count(456) == 1

