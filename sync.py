"""Compatibility entry point: ``python sync.py --cust-id ...``."""

from __future__ import annotations

import sys

from app.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["sync", *sys.argv[1:]]))

