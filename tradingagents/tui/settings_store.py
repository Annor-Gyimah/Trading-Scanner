"""Persistent app settings -- a small JSON file under ~/.tradingagents/,
same home-directory convention as favorites_store.py. Covers the handful of
cross-cutting settings the Settings modal exposes (paper-trading sizing,
testnet toggle, which tabs join auto-refresh) so they survive an app
restart instead of resetting to defaults every launch."""

from __future__ import annotations

import json
from pathlib import Path

SETTINGS_PATH = Path.home() / ".tradingagents" / "settings.json"

_DEFAULTS: dict = {
    "paper_leverage": 1.0,
    "paper_notional": 10.0,
    "testnet_enabled": False,
    "propr_enabled": False,
    "auto_refresh_tabs": None,  # None = app's own full default set, not yet customized
    "auto_refresh_tabs_seen": None,  # every tab id Settings has ever shown a switch for -- lets
    # a newly-added tab (e.g. Propr, Structure) tell apart "never seen, default it on" from "the
    # user explicitly switched this off"; see ScannerApp.__init__ for how it's used.
}


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return dict(_DEFAULTS)
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return dict(_DEFAULTS)
    merged = dict(_DEFAULTS)
    merged.update(data)
    return merged


def save_settings(settings: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")
