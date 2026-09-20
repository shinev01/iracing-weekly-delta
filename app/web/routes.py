"""FastAPI routes and dashboard context."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.analytics.weeks import dashboard_summary, every_race_points, weekly_summaries
from app.database.repository import RaceRepository
from app.models.normalized import CATEGORY_LABELS, category_label, normalize_category
from app.services.http import RemoteSourceError
from app.services.sync import SyncService


def create_app(db_path: str | Path = "iracing.db") -> FastAPI:
    """Build the local application with an explicit database dependency."""
    repository = RaceRepository(db_path)
    app = FastAPI(title="iRacing Weekly Tracker", version="0.1.0")
    app.state.repository = repository

    def web_progress(message: str) -> None:
        repository.set_meta("sync_progress", message)

    app.state.sync_service = SyncService(repository, progress=web_progress)
    sync_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sync")
    sync_lock = Lock()
    root = Path(__file__).resolve().parents[2]
    app.mount("/static", StaticFiles(directory=root / "static"), name="static")
    templates = Jinja2Templates(directory=root / "templates")

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        category: str = Query("sports_car"),
        season_id: int | None = Query(None),
        season_key: str | None = Query(None),
        season_name: str | None = Query(None),
        series_name: str | None = Query(None),
        car_name: str | None = Query(None),
        track_name: str | None = Query(None),
        sync_error: str | None = Query(None),
    ) -> HTMLResponse:
        cust_id = _configured_cust_id(repository)
        sync_running = bool(repository.get_meta("sync_running", False))
        visible_sync_error = (
            None if sync_running else sync_error or repository.get_meta("last_sync_error")
        )
        normalized_category = normalize_category(category) or "sports_car"
        if season_key is None and season_name:
            season_key = _season_key_from_name(season_name)
        season_year, season_quarter = _season_key_parts(season_key)
        races = (
            repository.list_races(
                cust_id,
                category=normalized_category,
                season_id=season_id,
                season_year=season_year,
                season_quarter=season_quarter,
                season_name=season_name if season_key is None else None,
                series_name=series_name,
                car_name=car_name,
                track_name=track_name,
            )
            if cust_id
            else []
        )
        weekly = weekly_summaries(races)
        season_options = repository.season_options(cust_id) if cust_id else []
        selected_season = next(
            (option for option in season_options if option["key"] == season_key),
            None,
        )
        context = {
            "request": request,
            "cust_id": cust_id,
            "category": normalized_category,
            "category_label": category_label(normalized_category),
            "category_options": [
                {"value": key, "label": value} for key, value in CATEGORY_LABELS.items()
            ],
            "season_id": season_id,
            "season_key": season_key,
            "season_name": season_name,
            "series_name": series_name,
            "car_name": car_name,
            "track_name": track_name,
            "season_options": season_options,
            "selected_season": selected_season,
            "season_not_downloaded": bool(selected_season and not selected_season["downloaded"]),
            "current_season_name": repository.get_meta("current_season_name"),
            "series_options": _options(repository, cust_id, "series_name"),
            "car_options": _options(repository, cust_id, "car_name"),
            "track_options": _options(repository, cust_id, "track_name"),
            "summary": dashboard_summary(races),
            "weekly": [summary.to_dict() for summary in weekly],
            "every_race": every_race_points(races),
            "debug": repository.latest_debug(cust_id) if cust_id else {},
            "index_sync_incomplete": bool(repository.get_meta("index_sync_incomplete", False)),
            "resume_page": (
                (repository.get_meta("last_successful_irstats_page", 0) or 0) + 1
            ),
            "sync_error": visible_sync_error,
            "sync_progress": repository.get_meta("sync_progress", ""),
            "sync_running": sync_running,
        }
        return templates.TemplateResponse(request, "index.html", context)

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "request": request,
                "cust_id": _configured_cust_id(repository) or "",
                "debug": repository.latest_debug(_configured_cust_id(repository))
                if _configured_cust_id(repository)
                else {},
                "index_sync_incomplete": bool(repository.get_meta("index_sync_incomplete", False)),
            },
        )

    @app.post("/settings")
    def save_settings(cust_id: str = Form(...)) -> RedirectResponse:
        validated = _parse_cust_id(cust_id)
        if validated is not None:
            repository.set_setting("cust_id", str(validated))
        return RedirectResponse(url="/", status_code=303)

    @app.post("/sync")
    def sync(mode: str = Form("quick"), season_key: str | None = Form(None)) -> RedirectResponse:
        cust_id = _configured_cust_id(repository)
        if cust_id is None:
            return RedirectResponse(url="/settings", status_code=303)

        if not sync_lock.acquire(blocking=False):
            return RedirectResponse(url="/", status_code=303)

        repository.set_meta("last_sync_error", None)
        repository.set_meta("sync_progress", "Connecting to iRStats...")
        repository.set_meta("sync_running", True)
        try:
            sync_executor.submit(
                _run_sync,
                app.state.sync_service,
                repository,
                sync_lock,
                cust_id,
                mode,
                season_key,
            )
        except Exception as exc:
            sync_lock.release()
            repository.set_meta("sync_running", False)
            repository.set_meta("last_sync_error", str(exc))
            repository.set_meta("sync_progress", "Sync failed.")
        return RedirectResponse(url="/", status_code=303)

    @app.get("/api/sync-status")
    def sync_status() -> JSONResponse:
        running = bool(repository.get_meta("sync_running", False))
        return JSONResponse(
            {
                "running": running,
                "message": repository.get_meta("sync_progress", "") or "",
                "error": None if running else repository.get_meta("last_sync_error"),
            }
        )

    @app.get("/debug", response_class=HTMLResponse)
    def debug(request: Request) -> HTMLResponse:
        cust_id = _configured_cust_id(repository)
        return templates.TemplateResponse(
            request,
            "debug.html",
            {
                "request": request,
                "cust_id": cust_id,
                "debug": repository.latest_debug(cust_id) if cust_id else {},
            },
        )

    @app.get("/api/races")
    def races_api(
        category: str = Query("sports_car"),
        season_id: int | None = Query(None),
        season_key: str | None = Query(None),
    ) -> JSONResponse:
        cust_id = _configured_cust_id(repository)
        if cust_id is None:
            return JSONResponse({"items": [], "count": 0})
        season_year, season_quarter = _season_key_parts(season_key)
        races = repository.list_races(
            cust_id,
            category=normalize_category(category),
            season_id=season_id,
            season_year=season_year,
            season_quarter=season_quarter,
        )
        return JSONResponse({"items": [race.to_dict() for race in races], "count": len(races)})

    @app.get("/api/debug")
    def debug_api() -> JSONResponse:
        cust_id = _configured_cust_id(repository)
        return JSONResponse(repository.latest_debug(cust_id) if cust_id else {})

    return app


def _run_sync(
    sync_service: SyncService,
    repository: RaceRepository,
    sync_lock: Lock,
    cust_id: int,
    mode: str,
    season_key: str | None,
) -> None:
    """Run one web sync outside the request thread and publish its final state."""
    try:
        if mode in {"full", "career", "resume"}:
            report = sync_service.sync(cust_id, full_rescan=True)
        elif mode in {"season", "rescan-season"}:
            report = sync_service.sync(
                cust_id,
                season_key=season_key,
                rescan_season=mode == "rescan-season",
            )
        else:
            report = sync_service.sync(cust_id)
        stopped_reason = getattr(report, "stopped_reason", None)
        if stopped_reason:
            repository.set_meta("last_sync_error", stopped_reason)
            repository.set_meta("sync_progress", stopped_reason)
        else:
            repository.set_meta("last_sync_error", None)
            repository.set_meta(
                "sync_progress",
                f"Sync complete. {getattr(report, 'imported', 0)} new races.",
            )
    except RemoteSourceError as exc:
        repository.set_meta("last_sync_error", str(exc))
        repository.set_meta("sync_progress", "Sync failed.")
    except Exception as exc:  # Keep background failures visible in the dashboard.
        repository.set_meta("last_sync_error", str(exc))
        repository.set_meta("sync_progress", "Sync failed.")
    finally:
        repository.set_meta("sync_running", False)
        sync_lock.release()


def _configured_cust_id(repository: RaceRepository) -> int | None:
    return _parse_cust_id(repository.get_setting("cust_id"))


def _parse_cust_id(value: str | None) -> int | None:
    try:
        parsed = int(str(value).strip()) if value is not None else None
    except ValueError:
        return None
    return parsed if parsed and parsed > 0 else None


def _options(repository: RaceRepository, cust_id: int | None, field: str) -> list[str]:
    return repository.filter_values(cust_id, field) if cust_id else []


def _season_key_parts(value: str | None) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    try:
        year_text, quarter_text = value.split("-", 1)
        year, quarter = int(year_text), int(quarter_text)
    except (AttributeError, ValueError):
        return None, None
    if 2000 <= year <= 2100 and 1 <= quarter <= 4:
        return year, quarter
    return None, None


def _season_key_from_name(value: str) -> str | None:
    words = value.replace("-", " ").split()
    for index, word in enumerate(words[:-2]):
        if (
            word.isdigit()
            and len(word) == 4
            and words[index + 1].casefold() == "season"
            and words[index + 2].isdigit()
        ):
            quarter = int(words[index + 2])
            if 1 <= quarter <= 4:
                return f"{int(word)}-{quarter}"
    return None
