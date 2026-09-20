from datetime import datetime, timezone
from threading import Event
from time import sleep

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
    assert "Rescan season" in response.text
    assert "Full rescan" not in response.text

    settings = client.get("/settings")
    assert settings.status_code == 200
    assert 'value="456"' in settings.text


def test_unimported_season_is_offered_without_starting_import(tmp_path) -> None:
    db_path = tmp_path / "web-season.db"
    repo = RaceRepository(db_path)
    repo.set_setting("cust_id", "456")
    repo.upsert_season_catalog(
        2,
        "Test Series - 2026 Season 3",
        "sports_car",
        {"season_id": 2, "season_name": "Test Series - 2026 Season 3"},
    )

    client = TestClient(create_app(db_path))
    response = client.get("/?season_key=2026-3")

    assert response.status_code == 200
    assert "2026 Season 3" in response.text
    assert "This season has not been downloaded yet." in response.text
    assert "Import season" in response.text


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
    assert response.headers["location"] == "/"

    for _ in range(100):
        status = client.get("/api/sync-status").json()
        if not status["running"]:
            break
        sleep(0.01)

    assert status["error"] == "public source returned HTTP 403"
    error_page = client.get("/")
    assert error_page.status_code == 200
    assert "public source returned HTTP 403" in error_page.text


def test_sync_starts_in_background_and_clears_previous_error(tmp_path) -> None:
    db_path = tmp_path / "web-sync-status.db"
    repo = RaceRepository(db_path)
    repo.set_setting("cust_id", "456")
    repo.set_meta("last_sync_error", "old sync failed")
    app = create_app(db_path)
    started = Event()
    release = Event()

    class BlockingSync:
        def sync(self, cust_id: int, *, full_rescan: bool = False) -> SyncReport:
            started.set()
            assert release.wait(2)
            return SyncReport(
                cust_id=cust_id,
                mode="quick",
                pages=1,
                found_subsessions=0,
                existing_locally=0,
                details_requested=0,
                imported=0,
                skipped_existing=0,
                failed=0,
            )

    app.state.sync_service = BlockingSync()
    client = TestClient(app)

    response = client.post("/sync", data={"mode": "quick"}, follow_redirects=False)

    assert response.status_code == 303
    assert started.wait(1)
    status = client.get("/api/sync-status").json()
    assert status == {
        "running": True,
        "message": "Connecting to iRStats...",
        "error": None,
    }
    assert "old sync failed" not in client.get("/").text

    release.set()
    for _ in range(100):
        status = client.get("/api/sync-status").json()
        if not status["running"]:
            break
        sleep(0.01)

    assert status["error"] is None
    assert status["message"] == "Sync complete. 0 new races."
