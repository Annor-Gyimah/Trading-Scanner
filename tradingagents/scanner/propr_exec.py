"""Propr (propr.xyz) prop-firm challenge auto-execution -- DRY RUN ONLY.

There is no separate Beta/sandbox API: confirmed against the real account
(GET /challenge-attempts returned exactly one attempt, period -- the
user's real $5,000 evaluation). Every order this module "places" is
logged, never sent. `execute_signal()`/`execute_signals()` build the
EXACT payload a real POST /accounts/{account_id}/orders call would use,
write it to a local SQLite log with status='dry_run', and return -- there
is no code path in this file that calls the orders endpoint. Flipping to
live execution is a deliberate, separate step for later, not a flag
here.

Key facts this module is built on (verified against the real account,
read-only, 2026-10-18):
  - Base URL https://api.propr.xyz/v1, auth via X-API-Key header.
  - Trades execute on Hyperliquid. Propr's own tradable set is NOT
    separately documented; fetch_tradable_bases() treats Hyperliquid's
    public perp universe as the source of truth, since that's what
    trades execute against.
  - Symbol namespace differs from the Binance scanner: Hyperliquid uses
    bare base tickers (DOGE, not DOGE/USDT:USDT) and a "k"-prefix
    convention for scaled-down meme tokens (kSHIB, kPEPE) where Binance
    uses a "1000"-prefix (1000SHIB, 1000PEPE). _map_to_base() converts.
  - Many of the scanner's own confirmed signals (Q, QNT, and most small/
    mid-cap tokens) are NOT on Hyperliquid at all -- confirmed directly:
    Q and QNT both tested False. Expect most signals to have no mapping.
  - Crypto leverage caps default to 2x; only BTC/ETH/SOL get up to 10x
    (GET /leverage-limits/effective) -- far lower than Binance. Sizing
    must respect this per-asset cap, not just paper_leverage.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

from .breakout import BreakoutSignal

DB_PATH = Path(os.environ.get("TRADINGAGENTS_PAPER_DB", str(Path.home() / ".tradingagents" / "paper_trades.db")))
PROPR_BASE_URL = os.environ.get("PROPR_API_URL", "https://api.propr.xyz/v1")
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"

DRY_RUN = True  # hardcoded -- see module docstring. Not a Settings toggle.

_schema_lock = threading.Lock()
_schema_ready = False
_tradable_bases_cache: set[str] | None = None


def credentials_configured() -> bool:
    return bool(os.environ.get("PROPR_API_KEY"))


def _headers() -> dict:
    return {"X-API-Key": os.environ["PROPR_API_KEY"]}


def fetch_tradable_bases(force: bool = False) -> set[str]:
    """Hyperliquid's public perp universe -- what Propr actually trades
    against. Cached per-process; pass force=True to refetch."""
    global _tradable_bases_cache
    if _tradable_bases_cache is not None and not force:
        return _tradable_bases_cache
    resp = requests.post(HYPERLIQUID_INFO_URL, json={"type": "meta"}, timeout=15)
    resp.raise_for_status()
    universe = resp.json().get("universe", [])
    _tradable_bases_cache = {u["name"] for u in universe}
    return _tradable_bases_cache


# Binance scales down sub-cent meme tokens with a "1000" prefix
# (1000SHIB, 1000PEPE, 1000BONK); Hyperliquid uses a "k" prefix for the
# same idea (kSHIB, kPEPE, kBONK). Map one to the other before checking
# tradability, or every scaled meme token looks unavailable when it
# usually isn't.
def _map_to_base(scanner_symbol: str) -> str:
    base = scanner_symbol.split("/")[0]
    if base.startswith("1000") and base[4:].isalpha():
        return "k" + base[4:]
    return base


def resolve_tradable_base(scanner_symbol: str, tradable_bases: set[str] | None = None) -> str | None:
    """The Hyperliquid base ticker for this scanner symbol, or None if
    it isn't tradable there at all (common -- most small/mid-caps
    aren't; Q and QNT both confirmed absent)."""
    bases = tradable_bases if tradable_bases is not None else fetch_tradable_bases()
    base = _map_to_base(scanner_symbol)
    return base if base in bases else None


def get_leverage_cap(base: str) -> float:
    """Max leverage Propr allows for this asset right now. Crypto
    defaults to 2x; only a few majors (BTC/ETH/SOL as of this writing)
    get a higher override."""
    resp = requests.get(f"{PROPR_BASE_URL}/leverage-limits/effective", headers=_headers(), timeout=15)
    resp.raise_for_status()
    data = resp.json()
    overrides = data.get("overrides", {})
    if base in overrides:
        return float(overrides[base])
    return float(data.get("defaults", {}).get("crypto", 2))


def get_active_account() -> dict | None:
    """The account embedded in the sole active challenge attempt (there
    is exactly one on this account, verified directly -- no separate
    Beta/demo account exists to pick between)."""
    resp = requests.get(f"{PROPR_BASE_URL}/challenge-attempts", headers=_headers(), params={"status": "active"}, timeout=15)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    if not data:
        return None
    return data[0].get("account")


@dataclass
class ProprExecutionResult:
    symbol: str  # original scanner symbol, e.g. "DOGE/USDT:USDT"
    base: str | None  # resolved Hyperliquid base ticker, or None if untradable there
    direction: str
    status: str  # "dry_run" | "skipped_untradable" | "failed"
    qty: float | None = None
    entry_price: float | None = None
    stop_price: float | None = None
    tp_price: float | None = None
    leverage: float | None = None
    error: str | None = None


_NEW_COLUMNS = [
    ("exit_price", "REAL"),
    ("exit_reason", "TEXT"),
    ("closed_at", "TEXT"),
    ("r_multiple", "REAL"),
    ("pnl_usd", "REAL"),
]


def _connect() -> sqlite3.Connection:
    global _schema_ready
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    if _schema_ready:
        return conn
    with _schema_lock:
        if _schema_ready:
            return conn
        _init_schema(conn)
        _schema_ready = True
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS propr_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            base TEXT,
            timeframe TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_time TEXT NOT NULL,
            qty REAL,
            leverage REAL,
            entry_price REAL,
            stop_price REAL,
            tp_price REAL,
            status TEXT NOT NULL DEFAULT 'dry_run',
            error TEXT,
            created_at TEXT NOT NULL,
            exit_price REAL,
            exit_reason TEXT,
            closed_at TEXT,
            UNIQUE(symbol, timeframe, direction, entry_time)
        )
        """
    )
    for name, decl in _NEW_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE propr_orders ADD COLUMN {name} {decl}")
        except sqlite3.OperationalError:
            pass


_SELECT_COLUMNS = (
    "id, symbol, base, timeframe, direction, entry_time, qty, leverage, entry_price, stop_price, "
    "tp_price, status, error, created_at, exit_price, exit_reason, closed_at, r_multiple, pnl_usd"
)
_COLUMN_NAMES = [c.strip() for c in _SELECT_COLUMNS.split(",")]


def execute_signal(
    signal: BreakoutSignal, timeframe: str, leverage: float = 1.0, margin_usd: float = 10.0,
) -> ProprExecutionResult:
    """Build the exact order this Confirmed signal WOULD place on Propr,
    log it with status='dry_run', and return -- never calls the real
    orders endpoint. See module docstring."""
    base = resolve_tradable_base(signal.symbol)
    if base is None:
        return ProprExecutionResult(
            symbol=signal.symbol, base=None, direction=signal.direction, status="skipped_untradable",
            error=f"{signal.symbol} is not tradable on Hyperliquid (Propr's execution venue)",
        )

    try:
        cap = get_leverage_cap(base)
    except Exception as exc:
        return ProprExecutionResult(
            symbol=signal.symbol, base=base, direction=signal.direction, status="failed",
            error=f"leverage-cap lookup failed: {exc}",
        )
    effective_leverage = min(leverage, cap)
    notional = margin_usd * effective_leverage
    qty = notional / signal.close if signal.close > 0 else 0.0
    stop_price = signal.stop_loss
    tp_price = signal.take_profits[0] if signal.take_profits else None

    entry_time = signal.entry_time.to_pydatetime().replace(tzinfo=timezone.utc) if signal.entry_time is not None else datetime.now(timezone.utc)
    now = datetime.now(timezone.utc).isoformat()

    with closing(_connect()) as conn:
        try:
            conn.execute(
                """INSERT INTO propr_orders
                   (symbol, base, timeframe, direction, entry_time, qty, leverage, entry_price,
                    stop_price, tp_price, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'dry_run', ?)""",
                (
                    signal.symbol, base, timeframe, signal.direction, entry_time.isoformat(),
                    round(qty, 6), effective_leverage, signal.close, stop_price, tp_price, now,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass  # already logged this exact signal -- no-op, same convention as record_signal/testnet_exec

    return ProprExecutionResult(
        symbol=signal.symbol, base=base, direction=signal.direction, status="dry_run",
        qty=round(qty, 6), entry_price=signal.close, stop_price=stop_price, tp_price=tp_price,
        leverage=effective_leverage,
    )


def execute_signals(
    signals: list[BreakoutSignal], timeframe: str, leverage: float = 1.0, margin_usd: float = 10.0,
) -> list[ProprExecutionResult]:
    return [execute_signal(s, timeframe, leverage, margin_usd) for s in signals]


def get_propr_orders(limit: int = 200) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM propr_orders ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(zip(_COLUMN_NAMES, row)) for row in rows]


_TRIGGERED_STATUSES_NOTE = "dry-run resolution compares logged stop/tp against live Hyperliquid mark price, not a real fill"


def resolve_propr_orders(limit: int = 200) -> list[dict]:
    """For every still-open ('dry_run') logged order, fetch Hyperliquid's
    current mark price for that base and check whether it has crossed the
    logged stop or TP -- a realistic read on what WOULD have happened,
    purely from observed price, with no Propr write access involved."""
    changed: list[dict] = []
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM propr_orders WHERE status = 'dry_run'"
        ).fetchall()
        if not rows:
            return changed
        try:
            resp = requests.post(HYPERLIQUID_INFO_URL, json={"type": "allMids"}, timeout=15)
            resp.raise_for_status()
            mids = resp.json()
        except Exception:
            return changed

        for row in rows:
            o = dict(zip(_COLUMN_NAMES, row))
            base = o["base"]
            mark = mids.get(base)
            if mark is None or o["stop_price"] is None:
                continue
            mark = float(mark)
            direction = o["direction"]
            hit_stop = mark <= o["stop_price"] if direction == "long" else mark >= o["stop_price"]
            hit_tp = (o["tp_price"] is not None) and (
                mark >= o["tp_price"] if direction == "long" else mark <= o["tp_price"]
            )
            if not hit_stop and not hit_tp:
                continue
            exit_reason = "stop" if hit_stop else "tp"
            exit_price = o["stop_price"] if hit_stop else o["tp_price"]

            # Same R/$PnL formula as paper_trading.py and testnet_exec.py,
            # so all three tabs' numbers mean exactly the same thing.
            sign = 1 if direction == "long" else -1
            risk = abs(o["entry_price"] - o["stop_price"]) if o["entry_price"] is not None else 0
            r_multiple = ((exit_price - o["entry_price"]) * sign) / risk if risk > 0 else 0.0
            pnl_usd = o["qty"] * (exit_price - o["entry_price"]) * sign if o["qty"] is not None else None

            closed_at = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE propr_orders SET status=?, exit_reason=?, exit_price=?, closed_at=?, "
                "r_multiple=?, pnl_usd=? WHERE symbol=? AND timeframe=? AND direction=? AND entry_time=?",
                (f"dry_closed_{exit_reason}", exit_reason, exit_price, closed_at,
                 round(r_multiple, 2), round(pnl_usd, 2) if pnl_usd is not None else None,
                 o["symbol"], o["timeframe"], o["direction"], o["entry_time"]),
            )
            o["status"], o["exit_reason"], o["exit_price"], o["closed_at"] = (
                f"dry_closed_{exit_reason}", exit_reason, exit_price, closed_at,
            )
            o["r_multiple"] = round(r_multiple, 2)
            o["pnl_usd"] = round(pnl_usd, 2) if pnl_usd is not None else None
            changed.append(o)
        conn.commit()
    return changed


def backfill_r_pnl() -> int:
    """Same purpose as testnet_exec.backfill_r_pnl() -- r_multiple/pnl_usd
    were added after this table already had closed rows. Safe to call
    repeatedly (only touches rows where r_multiple IS NULL)."""
    updated = 0
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT symbol, timeframe, direction, entry_time, qty, entry_price, stop_price, exit_price "
            "FROM propr_orders WHERE r_multiple IS NULL AND exit_price IS NOT NULL "
            "AND entry_price IS NOT NULL AND stop_price IS NOT NULL"
        ).fetchall()
        for symbol, timeframe, direction, entry_time, qty, entry_price, stop_price, exit_price in rows:
            sign = 1 if direction == "long" else -1
            risk = abs(entry_price - stop_price)
            if risk <= 0:
                continue
            r_multiple = (exit_price - entry_price) * sign / risk
            pnl_usd = qty * (exit_price - entry_price) * sign if qty is not None else None
            conn.execute(
                "UPDATE propr_orders SET r_multiple=?, pnl_usd=? "
                "WHERE symbol=? AND timeframe=? AND direction=? AND entry_time=?",
                (round(r_multiple, 2), round(pnl_usd, 2) if pnl_usd is not None else None,
                 symbol, timeframe, direction, entry_time),
            )
            updated += 1
        conn.commit()
    return updated


@dataclass
class ProprStats:
    total: int
    open: int
    wins: int
    losses: int
    win_rate: float  # of resolved (tp/stop) orders only
    avg_r: float
    total_r: float
    total_pnl_usd: float


def get_propr_stats() -> ProprStats:
    """Same win-rate/R/$PnL math as paper_trading.get_stats() and
    testnet_exec.get_testnet_stats() -- all three tabs' numbers mean the
    same thing, computed the same way."""
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT status, exit_reason, r_multiple, pnl_usd FROM propr_orders").fetchall()
    total = len(rows)
    open_count = sum(1 for status, _, _, _ in rows if status == "dry_run")
    wins = sum(1 for _, reason, _, _ in rows if reason == "tp")
    losses = sum(1 for _, reason, _, _ in rows if reason == "stop")
    resolved_r = [r for _, reason, r, _ in rows if reason in ("tp", "stop") and r is not None]
    win_rate = wins / (wins + losses) * 100 if (wins + losses) else 0.0
    avg_r = sum(resolved_r) / len(resolved_r) if resolved_r else 0.0
    total_pnl_usd = sum(p for _, _, _, p in rows if p is not None)
    return ProprStats(
        total=total, open=open_count, wins=wins, losses=losses,
        win_rate=round(win_rate, 1), avg_r=round(avg_r, 2), total_r=round(sum(resolved_r), 2),
        total_pnl_usd=round(total_pnl_usd, 2),
    )
