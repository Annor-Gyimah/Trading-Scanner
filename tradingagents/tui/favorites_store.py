"""Persistent favorites store -- a small JSON file under ~/.tradingagents/,
matching the same home-directory convention the rest of the project uses for
logs/cache/memory (see default_config.py)."""

from __future__ import annotations

import json
from pathlib import Path

FAVORITES_PATH = Path.home() / ".tradingagents" / "favorites.json"


def load_favorites() -> dict[str, dict]:
    if not FAVORITES_PATH.exists():
        return {}
    try:
        return json.loads(FAVORITES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_favorites(favorites: dict[str, dict]) -> None:
    FAVORITES_PATH.parent.mkdir(parents=True, exist_ok=True)
    FAVORITES_PATH.write_text(json.dumps(favorites, indent=2), encoding="utf-8")
