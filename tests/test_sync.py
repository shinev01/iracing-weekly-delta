from datetime import datetime, timezone

from app.database.repository import RaceRepository
from app.models.normalized import RaceResult
from app.services.http import HTTPResponse, RateLimitError, ResilientHTTPClient
from app.services.iracingdata import IRacingDataRace, SeasonInfo
from app.services.irstats import IrstatsClient, IrstatsPage, IrstatsRaceIndex
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
    progress: list[str] = []
    service = SyncService(
        repo,
        ir_stats=FakeIrstats(),
        iracing_data=data,
        metadata=FakeMetadata(),
        progress=progress.append,
    )

    first = service.sync(456)
    second = service.sync(456)

    assert first.imported == 1
    assert second.details_requested == 0
    assert data.calls == 1
    assert repo.count(456) == 1
    assert "Connecting to iRStats..." in progress
    assert "Reading page 1..." in progress
    assert "Found 1 races." in progress
    assert "Fetching race details: 1 / 1" in progress
    assert "Saving..." in progress
    assert "Sync complete." in progress


def test_full_rescan_saves_index_and_resumes_after_irstats_429(tmp_path) -> None:
    repo = RaceRepository(tmp_path / "rate-limit.db")
    data = FakeData()

    class PaginatedIrstats:
        def __init__(self) -> None:
            self.starts: list[int] = []

        def iter_history(self, cust_id, *, start_page=1, max_pages=None):
            self.starts.append(start_page)
            if start_page == 1:
                yield IrstatsPage(
                    page=1,
                    per_page=50,
                    has_more=True,
                    total_pages=3,
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
                raise RateLimitError("https://irstats.com/driver/456/races?page=2")
            yield IrstatsPage(
                page=2,
                per_page=50,
                has_more=False,
                total_pages=3,
                races=[
                    IrstatsRaceIndex(
                        subsession_id=100,
                        series_name="Test Series",
                        category="sports_car",
                        raw={"subsession_id": 100},
                    )
                ],
                raw={},
            )

    irstats = PaginatedIrstats()
    service = SyncService(
        repo,
        ir_stats=irstats,
        iracing_data=data,
        metadata=FakeMetadata(),
        progress=lambda _: None,
    )

    first = service.sync(456, full_rescan=True)

    assert first.stopped_reason is not None
    assert "Completed pages: 1 / 3" in first.stopped_reason
    assert repo.get_meta("last_successful_irstats_page") == 1
    assert repo.get_meta("total_pages") == 3
    assert repo.get_meta("index_sync_incomplete") is True
    assert repo.irstats_index_count(456) == 1

    second = service.sync(456, full_rescan=True)

    assert irstats.starts == [1, 2]
    assert second.completed_pages == 2
    assert repo.get_meta("index_sync_incomplete") is False
    assert repo.irstats_index_count(456) == 2


def test_quick_sync_requests_only_first_irstats_page(tmp_path) -> None:
    repo = RaceRepository(tmp_path / "quick.db")

    class TwoPages:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int | None]] = []

        def iter_history(self, cust_id, *, start_page=1, max_pages=None):
            self.calls.append((start_page, max_pages))
            yield IrstatsPage(
                page=1,
                per_page=50,
                has_more=True,
                total_pages=15,
                races=[],
                raw={},
            )
            yield IrstatsPage(
                page=2,
                per_page=50,
                has_more=True,
                total_pages=15,
                races=[],
                raw={},
            )

    irstats = TwoPages()
    service = SyncService(
        repo,
        ir_stats=irstats,
        iracing_data=FakeData(),
        metadata=FakeMetadata(),
        progress=lambda _: None,
    )

    report = service.sync(456)

    assert report.pages == 1
    assert irstats.calls == [(1, 1)]


def test_initial_sync_stops_after_current_season_boundary(tmp_path) -> None:
    repo = RaceRepository(tmp_path / "initial-season.db")

    class CurrentMetadata(FakeMetadata):
        season_records = [{"season_id": 1, "season_name": "Test - 2026 Season 4"}]

        def current_season(self):
            return SeasonInfo(2026, 4, (1,))

    class ScopedIrstats:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def fetch_page(self, cust_id, page):
            del cust_id
            self.calls.append(page)
            assert page == 1
            return IrstatsPage(
                page=1,
                per_page=50,
                has_more=True,
                total_pages=4,
                races=[
                    IrstatsRaceIndex(subsession_id=1, series_name="Current"),
                    IrstatsRaceIndex(subsession_id=2, series_name="Older"),
                ],
                raw={},
            )

    class ScopedData(FakeData):
        def get_race(self, subsession_id, cust_id):
            self.calls += 1
            current = subsession_id == 1
            return IRacingDataRace(
                subsession_id=subsession_id,
                season_id=1 if current else 0,
                season_name="Test - 2026 Season 4" if current else "Test - 2026 Season 3",
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

    irstats = ScopedIrstats()
    data = ScopedData()
    report = SyncService(
        repo,
        ir_stats=irstats,
        iracing_data=data,
        metadata=CurrentMetadata(),
        progress=lambda _: None,
    ).sync(456)

    assert report.mode == "initial"
    assert report.pages == 1
    assert report.irstats_requests == 1
    assert report.details_requested == 2
    assert report.imported == 1
    assert irstats.calls == [1]
    assert repo.count(456) == 1


def test_quick_sync_imports_new_current_race_and_skips_older_page_tail(tmp_path) -> None:
    repo = RaceRepository(tmp_path / "quick-season.db")
    repo.upsert_race(
        RaceResult(
            subsession_id=1,
            cust_id=456,
            start_time_utc=datetime(2026, 9, 1, tzinfo=timezone.utc),
            season_id=1,
            season_name="Test - 2026 Season 4",
            season_year=2026,
            season_quarter=4,
            raw_iracingdata={"results": [{"simsession_name": "RACE"}]},
        )
    )

    class CurrentMetadata(FakeMetadata):
        season_records = [{"season_id": 1, "season_name": "Test - 2026 Season 4"}]

        def current_season(self):
            return SeasonInfo(2026, 4, (1,))

    class OnePage:
        def __init__(self):
            self.calls = []

        def fetch_page(self, cust_id, page):
            del cust_id
            self.calls.append(page)
            assert page == 1
            return IrstatsPage(
                page=1,
                per_page=50,
                has_more=True,
                races=[
                    IrstatsRaceIndex(subsession_id=1),
                    IrstatsRaceIndex(subsession_id=3),
                    IrstatsRaceIndex(subsession_id=4),
                ],
                raw={},
            )

    class Data:
        def __init__(self):
            self.calls = []

        def get_race(self, subsession_id, cust_id):
            self.calls.append(subsession_id)
            current = subsession_id == 3
            return IRacingDataRace(
                subsession_id=subsession_id,
                season_id=1 if current else 0,
                season_name="Test - 2026 Season 4" if current else "Test - 2026 Season 3",
                start_time_utc=datetime(2026, 9, 2, tzinfo=timezone.utc),
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

    irstats = OnePage()
    data = Data()
    report = SyncService(
        repo,
        ir_stats=irstats,
        iracing_data=data,
        metadata=CurrentMetadata(),
        progress=lambda _: None,
    ).sync(456)

    assert report.mode == "quick"
    assert report.irstats_requests == 1
    assert report.imported == 1
    assert irstats.calls == [1]
    assert data.calls == [3, 4]
    assert repo.count(456) == 2


def test_historical_season_import_searches_through_newer_pages(tmp_path) -> None:
    repo = RaceRepository(tmp_path / "historical-season.db")

    class Metadata(FakeMetadata):
        season_records = [{"season_id": 1, "season_name": "Test - 2026 Season 4"}]

        def current_season(self):
            return SeasonInfo(2026, 4, (1,))

    class History:
        def __init__(self):
            self.calls = []

        def fetch_page(self, cust_id, page):
            del cust_id
            self.calls.append(page)
            if page == 1:
                races = [IrstatsRaceIndex(subsession_id=10)]
            else:
                races = [
                    IrstatsRaceIndex(subsession_id=11),
                    IrstatsRaceIndex(subsession_id=12),
                ]
            return IrstatsPage(
                page=page,
                per_page=50,
                has_more=page == 1,
                races=races,
                raw={},
            )

    class Data:
        def __init__(self):
            self.calls = []

        def get_race(self, subsession_id, cust_id):
            self.calls.append(subsession_id)
            season = {
                10: (1, "Test - 2026 Season 4"),
                11: (2, "Test - 2026 Season 3"),
                12: (0, "Test - 2026 Season 2"),
            }[subsession_id]
            return IRacingDataRace(
                subsession_id=subsession_id,
                season_id=season[0],
                season_name=season[1],
                start_time_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
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

    history = History()
    data = Data()
    report = SyncService(
        repo,
        ir_stats=history,
        iracing_data=data,
        metadata=Metadata(),
        progress=lambda _: None,
    ).sync(456, season_key="2026-3")

    assert report.mode == "season"
    assert history.calls == [1, 2]
    assert data.calls == [10, 11, 12]
    assert report.imported == 1
    assert repo.count(456) == 1


def test_full_rescan_stops_on_irstats_429_without_immediate_retry(tmp_path) -> None:
    repo = RaceRepository(tmp_path / "automatic-rate-limit.db")
    responses = {
        1: [HTTPResponse(200, {}, _sync_history_html(1))],
        2: [HTTPResponse(200, {}, _sync_history_html(2))],
        3: [
            HTTPResponse(429, {}, b"Too Many Requests"),
        ],
        4: [HTTPResponse(200, {}, _sync_history_html(4, total_pages=4))],
    }
    calls: list[int] = []
    sleeps: list[float] = []

    def transport(url: str, headers: dict[str, str], timeout: float) -> HTTPResponse:
        del headers, timeout
        page = int(url.rsplit("=", 1)[1])
        calls.append(page)
        return responses[page].pop(0)

    progress: list[str] = []
    service = SyncService(
        repo,
        ir_stats=IrstatsClient(
            http=ResilientHTTPClient(transport=transport),
            browser=None,
            page_delay=0,
            sleeper=sleeps.append,
            progress=progress.append,
        ),
        iracing_data=FakeData(),
        metadata=FakeMetadata(),
        progress=progress.append,
    )

    report = service.sync(456, full_rescan=True)

    assert report.stopped_reason is not None
    assert report.pages == 2
    assert repo.get_meta("last_successful_irstats_page") == 2
    assert repo.get_meta("index_sync_incomplete") is True
    assert calls == [1, 2, 3]
    assert sleeps == []


def _sync_history_html(page: int, *, total_pages: int = 4) -> bytes:
    next_link = (
        f'<a rel="next" href="/driver/456/races?page={page + 1}">Next</a>'
        if page < total_pages
        else ""
    )
    return f"""
        <p>Showing {(page - 1) * 50 + 1}–{page * 50} of {total_pages * 50} races</p>
        {next_link}
        <table><tbody><tr>
          <td><a href="/race/{9000 + page}">Race {page}</a></td>
        </tr></tbody></table>
    """.encode("utf-8")
