"""File-backed snapshot persistence.

Each ticker gets one JSON file at ``data/snapshots/{TICKER}.json``. This is
deliberately simple — the shape maps cleanly onto S3 / Blob / DynamoDB without
code changes (one object per ticker, write-once-per-refresh, read on dashboard
hit). For a production deployment you'd swap the read/write functions for the
chosen backend; the API surface stays the same.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import SNAPSHOT_DIR
from .schemas import TickerSnapshot


def _ensure_dir() -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)


def snapshot_path(ticker: str) -> Path:
    return SNAPSHOT_DIR / f"{ticker.upper()}.json"


def save_snapshot(snap: TickerSnapshot) -> None:
    _ensure_dir()
    path = snapshot_path(snap.ticker)
    payload = snap.model_dump(mode="json")
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def load_snapshot(ticker: str) -> Optional[TickerSnapshot]:
    path = snapshot_path(ticker)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return TickerSnapshot.model_validate(data)
    except (json.JSONDecodeError, ValueError):
        return None


def snapshot_age_seconds(ticker: str) -> Optional[float]:
    snap = load_snapshot(ticker)
    if snap is None:
        return None
    return (datetime.utcnow() - snap.refreshed_at.replace(tzinfo=None)).total_seconds()
