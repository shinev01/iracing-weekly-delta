"""Command-line entry points."""

from __future__ import annotations

import argparse
from pathlib import Path

from app.database.repository import RaceRepository
from app.services.sync import SyncService
from app.web.routes import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local iRacing Weekly Tracker")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync = subparsers.add_parser("sync", help="Import or quickly sync a driver's history")
    sync.add_argument("--cust-id", type=int, required=False)
    sync.add_argument("--db-path", default="iracing.db")
    sync.add_argument(
        "--full-rescan",
        action="store_true",
        help="Advanced: import the entire career (rate limits may require resume cycles)",
    )
    sync.add_argument(
        "--season-key",
        help="Import one season, for example 2026-3",
    )
    sync.add_argument(
        "--rescan-season",
        action="store_true",
        help="Recheck the selected season's index without redownloading complete details",
    )

    serve = subparsers.add_parser("serve", help="Run the local FastAPI UI")
    serve.add_argument("--db-path", default="iracing.db")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "sync":
        repository = RaceRepository(Path(args.db_path))
        cust_id = args.cust_id or _configured_cust_id(repository)
        if not cust_id:
            print("Provide --cust-id or save Customer ID at /settings.")
            return 2
        if args.full_rescan and args.season_key:
            parser.error("--full-rescan and --season-key are mutually exclusive")
        report = SyncService(repository).sync(
            cust_id,
            full_rescan=args.full_rescan,
            season_key=args.season_key,
            rescan_season=args.rescan_season,
        )
        print(
            f"iRStats requests: {report.irstats_requests}; "
            f"iRacingData detail requests: {report.details_requested}; "
            f"stored races: {report.races_stored}"
        )
        return 1 if report.stopped_reason else 0

    if args.command == "serve":
        import uvicorn

        uvicorn.run(create_app(args.db_path), host=args.host, port=args.port)
        return 0
    return 2


def _configured_cust_id(repository: RaceRepository) -> int | None:
    value = repository.get_setting("cust_id")
    try:
        parsed = int(value) if value else None
    except ValueError:
        return None
    return parsed if parsed and parsed > 0 else None
