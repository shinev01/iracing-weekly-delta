from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.database.repository import RaceRepository
from app.models.normalized import RaceResult
from app.services.sync import SyncReport
from app.web.routes import create_app


def test_dashboard_and_settings_render(tmp_path) -> None:
    db_path = tmp_path / "web.db"
    repo = RaceRepository(db_path)
    repo.set_setting("cust_id", "456")
    repo.upsert_race(
        RaceResult(
            subsession_id=1,
            cust_id=456,
            start_time_utc=datetime(2026, 9, 1, tzinfo=timezone.utc),
            season_id=1,
            season_name="Test Series - 2026 Season 4",
            series_name="Test Series",
            category="sports_car",
            track_name="Test Track",
            car_name="Test Car",
            old_irating=3000,
            new_irating=3040,
            start_position=3,
            finish_position=1,
            start_position_api=2,
            finish_position_api=0,
            incidents=0,
            sof=3000,
            season_year=2026,
            season_quarter=4,
            race_week_num=0,
            raw_iracingdata={"results": [{"simsession_name": "RACE"}]},
        )
    )

    client = TestClient(create_app(db_path))

    response = client.get("/")
    assert response.status_code == 200
    assert "Current iRating" in response.text
    assert "3040" in response.text
    assert "Test Track" in response.text

    settings = client.get("/settings")
    assert settings.status_code == 200
    assert 'value="456"' in settings.text


def test_sync_displays_a_stopped_source_error(tmp_path) -> None:
    db_path = tmp_path / "web-sync-error.db"
    repo = RaceRepository(db_path)
    repo.set_setting("cust_id", "456")
    app = create_app(db_path)

    class StoppedSync:
        def sync(self, cust_id: int, *, full_rescan: bool = False) -> SyncReport:
            assert cust_id == 456
            assert full_rescan is True
            return SyncReport(
                cust_id=456,
                mode="full",
                pages=0,
                found_subsessions=0,
                existing_locally=0,
                details_requested=0,
                imported=0,
                skipped_existing=0,
                failed=0,
                stopped_reason="public source returned HTTP 403",
            )

    app.state.sync_service = StoppedSync()
    client = TestClient(app)

    response = client.post("/sync", data={"mode": "full"}, follow_redirects=False)

    assert response.status_code == 303
    assert "sync_error=public%20source%20returned%20HTTP%20403" in response.headers["location"]

    error_page = client.get(response.headers["location"])
    assert error_page.status_code == 200
    assert "public source returned HTTP 403" in error_page.text
