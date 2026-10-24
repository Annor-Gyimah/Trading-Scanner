"""Cross-ledger analytics, read-only: regime breakdown and equity curves
over the same SQLite file's trades/testnet_orders/propr_orders tables.
Never writes to any of the three tables it reads from -- purely for the
Stats tab to answer "is this system's edge regime-dependent" and "what does
the PnL curve actually look like," questions the per-ledger stats functions
(get_stats/get_testnet_stats/get_propr_stats) don't answer on their own.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass

from .paper_trading import DB_PATH

LEDGERS = ("paper", "testnet", "propr")

_QUERIES = {
    "paper": ("trades", "closed = 1", "exit_time"),
    "testnet": ("testnet_orders", "status LIKE 'closed_%'", "closed_at"),
    "propr": ("propr_orders", "status LIKE 'dry_closed_%'", "closed_at"),
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@dataclass
class RegimeBucket:
    label: str  # "bullish" | "neutral" | "bearish" | "unknown" (logged before this column existed)
    n: int
    wins: int
    losses: int
    win_rate: float
    total_r: float
    total_pnl_usd: float


def get_regime_breakdown() -> dict[str, list[RegimeBucket]]:
    """One list of RegimeBucket per ledger, grouped by the regime_label
    snapshot stored at entry time. Trades logged before that column existed
    have regime_label=NULL and group under 'unknown' -- expected for
    anything already in the ledger, not a bug."""
    out: dict[str, list[RegimeBucket]] = {}
    with closing(_connect()) as conn:
        for ledger, (table, status_filter, _time_col) in _QUERIES.items():
            rows = conn.execute(
                f"SELECT COALESCE(NULLIF(regime_label, ''), 'unknown') AS regime, "
                f"exit_reason, r_multiple, pnl_usd FROM {table} WHERE {status_filter}"
            ).fetchall()
            buckets: dict[str, dict] = {}
            for regime, exit_reason, r_multiple, pnl_usd in rows:
                b = buckets.setdefault(
                    regime, {"n": 0, "wins": 0, "losses": 0, "total_r": 0.0, "total_pnl": 0.0}
                )
                b["n"] += 1
                if (exit_reason or "").startswith("tp"):
                    b["wins"] += 1
                elif exit_reason == "stop":
                    b["losses"] += 1
                b["total_r"] += r_multiple or 0.0
                b["total_pnl"] += pnl_usd or 0.0
            result = []
            for regime, b in sorted(buckets.items()):
                resolved = b["wins"] + b["losses"]
                win_rate = round(100 * b["wins"] / resolved, 1) if resolved else 0.0
                result.append(RegimeBucket(
                    label=regime, n=b["n"], wins=b["wins"], losses=b["losses"], win_rate=win_rate,
                    total_r=round(b["total_r"], 2), total_pnl_usd=round(b["total_pnl"], 2),
                ))
            out[ledger] = result
    return out


def get_equity_curve(ledger: str) -> list[float]:
    """Cumulative $PnL, in the chronological order trades actually closed,
    for one ledger ('paper'|'testnet'|'propr'). Just the running totals --
    the Stats tab plots these against trade sequence number, not wall-clock
    time, since trades don't close at evenly-spaced intervals."""
    table, status_filter, time_col = _QUERIES[ledger]
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"SELECT pnl_usd FROM {table} WHERE {status_filter} AND pnl_usd IS NOT NULL "
            f"ORDER BY {time_col} ASC"
        ).fetchall()
    out = []
    cum = 0.0
    for (pnl,) in rows:
        cum += pnl
        out.append(round(cum, 2))
    return out
