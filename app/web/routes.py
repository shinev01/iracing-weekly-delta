"""FastAPI routes and dashboard context."""

from __future__ import annotations

import json
from pathlib import Path
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
    app.state.sync_service = SyncService(repository)
    root = Path(__file__).resolve().parents[2]
    app.mount("/static", StaticFiles(directory=root / "static"), name="static")
    templates = Jinja2Templates(directory=root / "templates")

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        category: str = Query("sports_car"),
        season_id: int | None = Query(None),
        season_name: str | None = Query(None),
        series_name: str | None = Query(None),
        car_name: str | None = Query(None),
        track_name: str | None = Query(None),
        sync_error: str | None = Query(None),
    ) -> HTMLResponse:
        cust_id = _configured_cust_id(repository)
        visible_sync_error = sync_error or repository.get_meta("last_sync_error")
        normalized_category = normalize_category(category) or "sports_car"
        races = (
            repository.list_races(
                cust_id,
                category=normalized_category,
                season_id=season_id,
                season_name=season_name,
                series_name=series_name,
                car_name=car_name,
                track_name=track_name,
            )
            if cust_id
            else []
        )
        weekly = weekly_summaries(races)
        context = {
            "request": request,
            "cust_id": cust_id,
            "category": normalized_category,
            "category_label": category_label(normalized_category),
            "category_options": [
                {"value": key, "label": value} for key, value in CATEGORY_LABELS.items()
            ],
            "season_id": season_id,
            "season_name": season_name,
            "series_name": series_name,
            "car_name": car_name,
            "track_name": track_name,
            "season_options": _options(repository, cust_id, "season_name"),
            "series_options": _options(repository, cust_id, "series_name"),
            "car_options": _options(repository, cust_id, "car_name"),
            "track_options": _options(repository, cust_id, "track_name"),
            "summary": dashboard_summary(races),
            "weekly": [summary.to_dict() for summary in weekly],
            "every_race": every_race_points(races),
            "debug": repository.latest_debug(cust_id) if cust_id else {},
            "sync_error": visible_sync_error,
            "sync_error_json": json.dumps(visible_sync_error) if visible_sync_error else "null",
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
            },
        )

    @app.post("/settings")
    def save_settings(cust_id: str = Form(...)) -> RedirectResponse:
        validated = _parse_cust_id(cust_id)
        if validated is not None:
            repository.set_setting("cust_id", str(validated))
        return RedirectResponse(url="/", status_code=303)

    @app.post("/sync")
    def sync(mode: str = Form("quick")) -> RedirectResponse:
        cust_id = _configured_cust_id(repository)
        if cust_id is None:
            return RedirectResponse(url="/settings", status_code=303)
        try:
            report = app.state.sync_service.sync(cust_id, full_rescan=mode == "full")
        except RemoteSourceError as exc:
            repository.set_meta("last_sync_error", str(exc))
            return RedirectResponse(url=f"/?sync_error={_quote(str(exc))}", status_code=303)
        if report.stopped_reason:
            repository.set_meta("last_sync_error", report.stopped_reason)
            return RedirectResponse(
                url=f"/?sync_error={_quote(report.stopped_reason)}",
                status_code=303,
            )
        return RedirectResponse(url="/", status_code=303)

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
    ) -> JSONResponse:
        cust_id = _configured_cust_id(repository)
        if cust_id is None:
            return JSONResponse({"items": [], "count": 0})
        races = repository.list_races(cust_id, category=normalize_category(category), season_id=season_id)
        return JSONResponse({"items": [race.to_dict() for race in races], "count": len(races)})

    @app.get("/api/debug")
    def debug_api() -> JSONResponse:
        cust_id = _configured_cust_id(repository)
        return JSONResponse(repository.latest_debug(cust_id) if cust_id else {})

    return app


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


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")
