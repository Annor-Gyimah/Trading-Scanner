"""Optional auto-execution of CONFIRMED signals against Binance Futures
TESTNET (testnet.binancefuture.com) -- a real exchange matching engine with
fake funds, not Binance's live mainnet, no real money ever at risk.

Why this exists alongside the in-app paper-trading ledger: that ledger's
R/PnL math assumes a perfect fill at the exact stop/target price, which is
optimistic. A real testnet order goes through the actual matching engine --
real slippage, real partial fills, a real stop/take-profit order lifecycle
-- so it's a materially more honest test of whether a signal is actually
tradeable, not just mathematically correct on paper.

Entirely opt-in, off by default, and crypto-only (Binance Futures Testnet
lists the same USDT-M perpetuals the live scanner already scans for
market="crypto" -- not tokenized stock perpetuals, not real stocks):

  1. Create a Binance Futures TESTNET account + API key at
     https://testnet.binancefuture.com/ (separate signup from real Binance,
     pre-loaded with fake USDT, nothing to do with your real account).
  2. Set BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET.
  3. Turn on "Testnet auto-execution" in the TUI's Settings (press s) or
     pass --testnet-execute on the CLI.

Without both env vars set, every call here raises a clear error instead of
silently doing nothing or, worse, accidentally trying mainnet.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import ccxt

from .breakout import BreakoutSignal

DB_PATH = Path(os.environ.get("TRADINGAGENTS_PAPER_DB", str(Path.home() / ".tradingagents" / "paper_trades.db")))

_exchange: ccxt.binanceusdm | None = None

_NEW_COLUMNS = [
    ("entry_price", "REAL"),
    ("stop_price", "REAL"),
    ("tp_price", "REAL"),
    ("exit_price", "REAL"),
    ("exit_reason", "TEXT"),  # "tp" | "stop" | "manual" (closed some other way)
    ("closed_at", "TEXT"),
    ("r_multiple", "REAL"),
    ("pnl_usd", "REAL"),
]


def credentials_configured() -> bool:
    return bool(os.environ.get("BINANCE_TESTNET_API_KEY") and os.environ.get("BINANCE_TESTNET_API_SECRET"))


def get_testnet_exchange() -> ccxt.binanceusdm:
    """A single shared ccxt exchange instance, pointed at Binance Futures
    Testnet via ccxt's own sandbox-mode URL swap (never mainnet -- there is
    no code path here that can reach real funds)."""
    global _exchange
    if _exchange is not None:
        return _exchange
    api_key = os.environ.get("BINANCE_TESTNET_API_KEY")
    api_secret = os.environ.get("BINANCE_TESTNET_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError(
            "BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET not set -- create a Binance "
            "Futures Testnet account + API key at https://testnet.binancefuture.com/ and set both."
        )
    exchange = ccxt.binanceusdm({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True})
    # NOT set_sandbox_mode(True): ccxt/Binance deprecated testnet.binancefuture.com support
    # for futures and replaced it with "Demo Trading" (same account, same API keys --
    # https://www.binance.com/en/support/faq/detail/9be58f73e5e14338809e3b705b9687dd),
    # activated through this separate method. set_sandbox_mode raises NotSupported now.
    exchange.enable_demo_trading(True)
    _exchange = exchange
    return exchange


_schema_lock = threading.Lock()
_schema_ready = False


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
        CREATE TABLE IF NOT EXISTS testnet_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_time TEXT NOT NULL,
            qty REAL,
            leverage REAL,
            margin_usd REAL,
            entry_order_id TEXT,
            stop_order_id TEXT,
            tp_order_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            error TEXT,
            created_at TEXT NOT NULL,
            entry_price REAL,
            stop_price REAL,
            tp_price REAL,
            exit_price REAL,
            exit_reason TEXT,
            closed_at TEXT,
            UNIQUE(symbol, timeframe, direction, entry_time)
        )
        """
    )
    for name, decl in _NEW_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE testnet_orders ADD COLUMN {name} {decl}")
        except sqlite3.OperationalError:
            pass  # already has it -- harmless on a fresh table or an already-migrated one


@dataclass
class ExecutionResult:
    symbol: str
    direction: str
    status: str  # "placed" | "failed" | "skipped" (already executed)
    qty: float | None = None
    entry_order_id: str | None = None
    stop_order_id: str | None = None
    tp_order_id: str | None = None
    error: str | None = None


def execute_signal(
    signal: BreakoutSignal, timeframe: str, leverage: float = 1.0, margin_usd: float = 10.0,
) -> ExecutionResult:
    """Place a real (testnet) market entry plus reduce-only stop-loss and
    TP1 take-profit for one CONFIRMED signal. Single target (TP1), same
    simplification backtest.py uses, for the same reason: the staged TP1/2/3
    ladder needs partial-position accounting this MVP doesn't attempt yet.

    Deduplicates the same way record_signal does (symbol/timeframe/
    direction/entry_time) -- safe to call every scan cycle on the same
    results list without double-placing an order for a signal already seen.
    """
    if signal.entry_time is None:
        return ExecutionResult(signal.symbol, signal.direction, "skipped", error="signal has no entry_time")
    entry_time_iso = signal.entry_time.to_pydatetime().replace(tzinfo=timezone.utc).isoformat()

    with closing(_connect()) as conn:
        try:
            conn.execute(
                """INSERT INTO testnet_orders
                   (symbol, timeframe, direction, entry_time, leverage, margin_usd, created_at,
                    entry_price, stop_price, tp_price)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (signal.symbol, timeframe, signal.direction, entry_time_iso, leverage, margin_usd,
                 datetime.now(timezone.utc).isoformat(), signal.close, signal.stop_loss, signal.take_profits[0]),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return ExecutionResult(signal.symbol, signal.direction, "skipped", error="already executed")

        try:
            exchange = get_testnet_exchange()
            exchange.load_markets()
            try:
                exchange.set_leverage(int(leverage), signal.symbol)
            except Exception:
                pass  # leverage may already be set to this value -- not fatal either way

            position_usd = margin_usd * leverage
            raw_qty = position_usd / signal.close
            qty = float(exchange.amount_to_precision(signal.symbol, raw_qty))
            if qty <= 0:
                raise ValueError(f"computed order quantity rounds to 0 for {signal.symbol} at this margin/leverage")

            side = "buy" if signal.direction == "long" else "sell"
            opposite_side = "sell" if signal.direction == "long" else "buy"

            entry_order = exchange.create_order(signal.symbol, "market", side, qty)
            stop_order = exchange.create_order(
                signal.symbol, "market", opposite_side, qty,
                params={"stopLossPrice": exchange.price_to_precision(signal.symbol, signal.stop_loss), "reduceOnly": True},
            )
            tp_order = exchange.create_order(
                signal.symbol, "market", opposite_side, qty,
                params={"takeProfitPrice": exchange.price_to_precision(signal.symbol, signal.take_profits[0]), "reduceOnly": True},
            )

            conn.execute(
                """UPDATE testnet_orders SET qty=?, entry_order_id=?, stop_order_id=?, tp_order_id=?, status='placed'
                   WHERE symbol=? AND timeframe=? AND direction=? AND entry_time=?""",
                (qty, str(entry_order.get("id")), str(stop_order.get("id")), str(tp_order.get("id")),
                 signal.symbol, timeframe, signal.direction, entry_time_iso),
            )
            conn.commit()
            return ExecutionResult(
                signal.symbol, signal.direction, "placed", qty=qty,
                entry_order_id=str(entry_order.get("id")), stop_order_id=str(stop_order.get("id")),
                tp_order_id=str(tp_order.get("id")),
            )
        except Exception as exc:
            conn.execute(
                """UPDATE testnet_orders SET status='failed', error=?
                   WHERE symbol=? AND timeframe=? AND direction=? AND entry_time=?""",
                (str(exc), signal.symbol, timeframe, signal.direction, entry_time_iso),
            )
            conn.commit()
            return ExecutionResult(signal.symbol, signal.direction, "failed", error=str(exc))


def execute_signals(
    results: list[BreakoutSignal], timeframe: str, leverage: float = 1.0, margin_usd: float = 10.0,
) -> list[ExecutionResult]:
    """Run execute_signal over every result, skipping anything already
    executed. Returns only the outcomes for signals this call actually
    touched (placed or newly failed) -- repeats of an already-seen signal
    come back as "skipped" but are filtered out of the returned list so
    callers only see what's new."""
    out = []
    for s in results:
        result = execute_signal(s, timeframe, leverage=leverage, margin_usd=margin_usd)
        if result.status != "skipped":
            out.append(result)
    return out


_SELECT_COLUMNS = (
    "symbol, timeframe, direction, entry_time, qty, leverage, margin_usd, entry_order_id, "
    "stop_order_id, tp_order_id, status, error, created_at, entry_price, stop_price, tp_price, "
    "exit_price, exit_reason, closed_at, r_multiple, pnl_usd"
)
_COLUMN_NAMES = [c.strip() for c in _SELECT_COLUMNS.split(",")]


def get_testnet_orders(limit: int = 200) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM testnet_orders ORDER BY created_at DESC LIMIT ?", (limit,),
        ).fetchall()
    return [dict(zip(_COLUMN_NAMES, row)) for row in rows]


# Binance's raw algoStatus values that mean "this conditional order actually
# triggered and filled" -- NOT "NEW"/"ACCEPTED"/"TRIGGERING" (still resting)
# and NOT "CANCELED"/"EXPIRED"/"REJECTED" (ended without firing). See ccxt's
# binance.parse_order_status for the full mapping this mirrors.
_TRIGGERED_STATUSES = {"FILLED", "TRIGGERED", "FINISHED"}


def _algo_status(exchange: ccxt.binanceusdm, algo_id: str | None) -> str | None:
    """Raw Binance algoStatus for one conditional (stop/TP) order, or None if
    it can't be looked up (bad id, already purged from history, etc.)."""
    if not algo_id:
        return None
    try:
        resp = exchange.fapiPrivateGetAlgoOrder({"algoId": algo_id})
        return resp.get("algoStatus")
    except Exception:
        return None


def resolve_testnet_orders(limit: int = 200) -> list[dict]:
    """Re-check every 'placed' order against the real account: is the
    position still open, or did the stop or TP actually trigger. Call this
    periodically (the Testnet tab does, on each visit/auto-refresh) --
    nothing resolves between calls on its own. Returns the rows that
    changed status this call.
    """
    if not credentials_configured():
        return []
    exchange = get_testnet_exchange()
    changed: list[dict] = []
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM testnet_orders WHERE status = 'placed'"
        ).fetchall()
        for row in rows:
            o = dict(zip(_COLUMN_NAMES, row))
            try:
                positions = exchange.fetch_positions([o["symbol"]])
            except Exception:
                continue  # one symbol failing to fetch shouldn't block the rest
            still_open = any(abs(p.get("contracts") or 0) > 0 for p in positions)
            if still_open:
                continue  # nothing to resolve yet

            tp_status = _algo_status(exchange, o["tp_order_id"])
            stop_status = _algo_status(exchange, o["stop_order_id"])
            if tp_status in _TRIGGERED_STATUSES:
                exit_reason, exit_price = "tp", o["tp_price"]
            elif stop_status in _TRIGGERED_STATUSES:
                exit_reason, exit_price = "stop", o["stop_price"]
            else:
                # Flat but neither tracked order shows as triggered -- closed
                # some other way (manually on the exchange, liquidated, etc).
                exit_reason, exit_price = "manual", None

            # Same R/$PnL formula as paper_trading.py's resolve_open_trades,
            # so all three tabs' numbers mean the same thing. "manual" closes
            # have no exit_price to compute from -- r_multiple/pnl_usd stay
            # None, excluded from stats the same way paper trading excludes
            # a None r from its win-rate/avg-R math.
            r_multiple = pnl_usd = None
            if exit_price is not None and o["entry_price"] is not None and o["stop_price"] is not None:
                sign = 1 if o["direction"] == "long" else -1
                risk = abs(o["entry_price"] - o["stop_price"])
                r_multiple = ((exit_price - o["entry_price"]) * sign) / risk if risk > 0 else 0.0
                if o["qty"] is not None:
                    pnl_usd = o["qty"] * (exit_price - o["entry_price"]) * sign

            closed_at = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE testnet_orders SET status=?, exit_reason=?, exit_price=?, closed_at=?, "
                "r_multiple=?, pnl_usd=? WHERE symbol=? AND timeframe=? AND direction=? AND entry_time=?",
                (f"closed_{exit_reason}", exit_reason, exit_price, closed_at,
                 round(r_multiple, 2) if r_multiple is not None else None,
                 round(pnl_usd, 2) if pnl_usd is not None else None,
                 o["symbol"], o["timeframe"], o["direction"], o["entry_time"]),
            )
            o["status"], o["exit_reason"], o["exit_price"], o["closed_at"] = (
                f"closed_{exit_reason}", exit_reason, exit_price, closed_at,
            )
            o["r_multiple"] = round(r_multiple, 2) if r_multiple is not None else None
            o["pnl_usd"] = round(pnl_usd, 2) if pnl_usd is not None else None
            changed.append(o)
        conn.commit()
    return changed


def backfill_r_pnl() -> int:
    """r_multiple/pnl_usd were added after this table already had closed
    rows -- their entry/stop/exit prices are still right there, so this
    computes what those two columns should always have been, once. Safe
    to call repeatedly (only touches rows where r_multiple IS NULL).
    Returns how many rows were updated."""
    updated = 0
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT symbol, timeframe, direction, entry_time, qty, entry_price, stop_price, exit_price "
            "FROM testnet_orders WHERE r_multiple IS NULL AND exit_price IS NOT NULL "
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
                "UPDATE testnet_orders SET r_multiple=?, pnl_usd=? "
                "WHERE symbol=? AND timeframe=? AND direction=? AND entry_time=?",
                (round(r_multiple, 2), round(pnl_usd, 2) if pnl_usd is not None else None,
                 symbol, timeframe, direction, entry_time),
            )
            updated += 1
        conn.commit()
    return updated


@dataclass
class TestnetStats:
    total: int
    open: int
    wins: int
    losses: int
    win_rate: float  # of resolved (tp/stop) orders only -- "manual" closes count toward total, neither
    avg_r: float
    total_r: float
    total_pnl_usd: float


def get_testnet_stats() -> TestnetStats:
    """Same win-rate/R/$PnL math as paper_trading.get_stats(), over testnet
    orders instead -- so the two tabs' numbers mean exactly the same thing."""
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT status, exit_reason, r_multiple, pnl_usd FROM testnet_orders").fetchall()
    total = len(rows)
    open_count = sum(1 for status, _, _, _ in rows if status == "placed")
    wins = sum(1 for _, reason, _, _ in rows if reason == "tp")
    losses = sum(1 for _, reason, _, _ in rows if reason == "stop")
    resolved_r = [r for _, reason, r, _ in rows if reason in ("tp", "stop") and r is not None]
    win_rate = wins / (wins + losses) * 100 if (wins + losses) else 0.0
    avg_r = sum(resolved_r) / len(resolved_r) if resolved_r else 0.0
    total_pnl_usd = sum(p for _, _, _, p in rows if p is not None)
    return TestnetStats(
        total=total, open=open_count, wins=wins, losses=losses,
        win_rate=round(win_rate, 1), avg_r=round(avg_r, 2), total_r=round(sum(resolved_r), 2),
        total_pnl_usd=round(total_pnl_usd, 2),
    )
