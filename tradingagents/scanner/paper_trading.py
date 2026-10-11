"""Paper-trading tracker: logs every CONFIRMED signal the live scanner fires,
then on each later check resolves it against real subsequent price action --
did it actually hit its stop or its targets. This is the honest answer to
"does the system know if its own signals were right": not a backtest replay
of history, a ledger of what it told you live, checked against what the
market then actually did.

Tracks the FULL staged TP1/TP2/TP3 ladder, not just a single target (unlike
backtest.py, which deliberately simplifies to one target for a quick
historical sanity check -- see that module's docstring). A live paper trade
isn't a one-shot replay; it's meant to mirror what you'd actually be watching
over days, so once TP1 is touched the trade stays open and keeps tracking
for TP2/TP3, locking in only once price either reaches TP3 (nothing higher to
chase) or falls back through the ORIGINAL stop. A trade that has touched any
target is never relabeled a loss afterward -- a real trader already banked
that profit; the exit recorded is the best target actually reached, not
wherever price wandered to later.

Persisted to a small SQLite file under ~/.tradingagents/ (same home-directory
convention as favorites_store.py) so the log survives across runs and
accumulates real history over time.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .backtest import is_valid_setup
from .binance_data import fetch_ohlcv_df
from .stock_data import fetch_stock_ohlcv_df

# Overridable via env var specifically so testing/dev work never has to touch
# (or accidentally wipe) the real accumulating ledger at the default path --
# a mistake made repeatedly during this module's own development, which is
# exactly the failure mode this exists to prevent going forward.
DB_PATH = Path(os.environ.get("TRADINGAGENTS_PAPER_DB", str(Path.home() / ".tradingagents" / "paper_trades.db")))

_NEW_COLUMNS = [
    ("target2", "REAL"),
    ("target3", "REAL"),
    ("best_level", "INTEGER NOT NULL DEFAULT 0"),
    ("closed", "INTEGER NOT NULL DEFAULT 0"),
    ("notional_usd", "REAL NOT NULL DEFAULT 10.0"),
    ("pnl_usd", "REAL"),
    ("leverage", "REAL NOT NULL DEFAULT 1.0"),
]


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            market TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            tier TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_time TEXT NOT NULL,
            entry_price REAL NOT NULL,
            stop_loss REAL NOT NULL,
            target REAL NOT NULL,
            target2 REAL,
            target3 REAL,
            score REAL,
            reason TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            best_level INTEGER NOT NULL DEFAULT 0,
            closed INTEGER NOT NULL DEFAULT 0,
            exit_time TEXT,
            exit_price REAL,
            exit_reason TEXT,
            r_multiple REAL,
            pnl_pct REAL,
            notional_usd REAL NOT NULL DEFAULT 10.0,
            pnl_usd REAL,
            leverage REAL NOT NULL DEFAULT 1.0,
            UNIQUE(symbol, market, timeframe, direction, entry_time)
        )
        """
    )
    # Migration path for a DB created before target2/target3/best_level/
    # closed/notional_usd/pnl_usd existed -- each ADD COLUMN fails harmlessly
    # with "duplicate column" on a DB that already has it (including a fresh
    # one just created above), which is exactly the no-op we want.
    for name, decl in _NEW_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {name} {decl}")
        except sqlite3.OperationalError:
            pass
    return conn


@dataclass
class PaperTrade:
    id: int
    symbol: str
    market: str
    timeframe: str
    tier: str
    direction: str
    entry_time: datetime
    entry_price: float
    stop_loss: float
    target1: float
    target2: float | None
    target3: float | None
    score: float | None
    reason: str | None
    status: str  # "open" | "won" | "lost"
    best_level: int  # 0 (nothing touched yet) .. 3 (TP3 reached)
    closed: bool  # True once nothing more can change (TP3 hit, or stopped out)
    exit_time: datetime | None
    exit_price: float | None  # the BEST target's price if won, or the stop if lost
    exit_reason: str | None  # "tp1" | "tp2" | "tp3" | "stop"
    r_multiple: float | None
    pnl_pct: float | None
    notional_usd: float  # margin per trade, in dollars
    leverage: float  # locked in at entry; changing the app-wide default later never touches this trade
    pnl_usd: float | None

    @property
    def targets(self) -> list[float]:
        return [t for t in (self.target1, self.target2, self.target3) if t is not None]

    @property
    def position_usd(self) -> float:
        """Total notional exposure this trade is sized for: margin x leverage."""
        return self.notional_usd * self.leverage


_SELECT_COLUMNS = (
    "id, symbol, market, timeframe, tier, direction, entry_time, entry_price, stop_loss, target, "
    "score, reason, status, exit_time, exit_price, exit_reason, r_multiple, pnl_pct, "
    "target2, target3, best_level, closed, notional_usd, pnl_usd, leverage"
)


def _row_to_trade(row: tuple) -> PaperTrade:
    (id_, symbol, market, timeframe, tier, direction, entry_time, entry_price, stop_loss, target,
     score, reason, status, exit_time, exit_price, exit_reason, r_multiple, pnl_pct,
     target2, target3, best_level, closed, notional_usd, pnl_usd, leverage) = row
    return PaperTrade(
        id=id_, symbol=symbol, market=market, timeframe=timeframe, tier=tier, direction=direction,
        entry_time=datetime.fromisoformat(entry_time), entry_price=entry_price, stop_loss=stop_loss,
        target1=target, target2=target2, target3=target3, score=score, reason=reason, status=status,
        best_level=best_level, closed=bool(closed),
        exit_time=datetime.fromisoformat(exit_time) if exit_time else None,
        exit_price=exit_price, exit_reason=exit_reason, r_multiple=r_multiple, pnl_pct=pnl_pct,
        notional_usd=notional_usd if notional_usd is not None else 10.0,
        leverage=leverage if leverage is not None else 1.0, pnl_usd=pnl_usd,
    )


def record_signal(
    symbol: str, market: str, timeframe: str, tier: str, direction: str,
    entry_time: datetime, entry_price: float, stop_loss: float,
    target1: float, target2: float | None = None, target3: float | None = None,
    score: float | None = None, reason: str | None = None,
    notional_usd: float = 10.0, leverage: float = 1.0,
) -> bool:
    """Log one live signal as a new open paper trade. Returns False (no-op)
    if this exact signal (same symbol/market/timeframe/direction/entry_time)
    was already logged -- harmless to call every scan cycle. Also skips a
    signal whose own stop/target1 aren't usable (see is_valid_setup).

    `leverage` is locked in at the moment this trade is logged -- it is NOT
    re-read from any later "current setting," so changing the app-wide
    default afterward only affects trades logged from then on, never
    retroactively changes one already in the ledger.
    """
    if not is_valid_setup(direction, entry_price, stop_loss, target1):
        return False
    with closing(_connect()) as conn:
        try:
            conn.execute(
                """INSERT INTO trades
                   (symbol, market, timeframe, tier, direction, entry_time, entry_price,
                    stop_loss, target, target2, target3, score, reason, notional_usd, leverage)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (symbol, market, timeframe, tier, direction, entry_time.isoformat(),
                 entry_price, stop_loss, target1, target2, target3, score, reason, notional_usd, leverage),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # already logged -- not an error, just a repeat scan


def _fetch_df_for(symbol: str, market: str, timeframe: str, limit: int):
    if market == "stocks-real":
        return fetch_stock_ohlcv_df(symbol, timeframe=timeframe, limit=limit)
    return fetch_ohlcv_df(symbol, timeframe=timeframe, limit=limit)


def _walk_levels(
    df, start_idx: int, direction: str, stop: float, targets: list[float],
) -> tuple[int, int | None, bool, int | None]:
    """Walk df from start_idx forward, tracking the highest target reached.

    Stop is checked before targets on any single bar (conservative -- intrabar
    ordering isn't knowable from OHLC alone). Reaching the top target ends the
    walk immediately (nothing higher to chase).

    Returns (best_level, level_hit_idx, hit_stop, end_idx):
      - best_level: 0..len(targets), the highest target touched.
      - level_hit_idx: row index where best_level was reached, or None if 0.
      - hit_stop: whether the ORIGINAL stop was touched at end_idx.
      - end_idx: the stop-hit row if hit_stop, the top-target row if maxed
        out, or None if the data ran out with the trade still trackable.
    """
    level = 0
    level_idx = None
    top = len(targets)
    for idx in range(start_idx, len(df)):
        row = df.iloc[idx]
        hit_stop = (row["low"] <= stop) if direction == "long" else (row["high"] >= stop)
        if hit_stop:
            return level, level_idx, True, idx
        for lvl in range(top, 0, -1):
            if level >= lvl:
                continue
            tgt = targets[lvl - 1]
            touched = (row["high"] >= tgt) if direction == "long" else (row["low"] <= tgt)
            if touched:
                level, level_idx = lvl, idx
                break
        if level == top:
            return level, level_idx, False, idx
    return level, level_idx, False, None


def resolve_open_trades(max_fetch_bars: int = 1500) -> list[PaperTrade]:
    """Re-check every not-yet-closed paper trade (open, or won but still
    tracking for a higher target) against fresh price data. Recomputes the
    full outcome from entry each time rather than resuming from a saved
    checkpoint -- simpler and self-healing, and the data volume is small
    enough that it's cheap. Call this periodically (e.g. once per
    auto-refresh cycle); nothing resolves on its own between calls. Returns
    every trade whose status/level/closed actually changed this call.
    """
    with closing(_connect()) as conn:
        open_rows = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM trades WHERE closed = 0 ORDER BY entry_time"
        ).fetchall()
        changed: list[PaperTrade] = []
        for row in open_rows:
            trade = _row_to_trade(row)
            try:
                df = _fetch_df_for(trade.symbol, trade.market, trade.timeframe, max_fetch_bars)
            except Exception:
                continue  # one symbol failing to fetch shouldn't block the rest

            # Both binance_data's and stock_data's timestamp columns are
            # tz-naive (implicitly UTC); entry_time is stored tz-aware (UTC).
            df_ts = df["timestamp"]
            if df_ts.dt.tz is not None:
                df_ts = df_ts.dt.tz_localize(None)
            entry_ts = trade.entry_time
            if entry_ts.tzinfo is not None:
                entry_ts = entry_ts.astimezone(timezone.utc).replace(tzinfo=None)
            after_entry = df_ts > entry_ts
            if not after_entry.any():
                continue  # no candles past entry yet
            start_idx = int(after_entry.values.argmax())

            targets = trade.targets
            level, level_idx, hit_stop, end_idx = _walk_levels(
                df, start_idx, trade.direction, trade.stop_loss, targets
            )

            if hit_stop and level == 0:
                status, closed = "lost", True
                exit_idx, exit_price, exit_reason = end_idx, trade.stop_loss, "stop"
            elif level == len(targets) and not hit_stop:
                status, closed = "won", True
                exit_idx, exit_price, exit_reason = level_idx, targets[level - 1], f"tp{level}"
            elif hit_stop and level >= 1:
                # Stopped out AFTER already banking a target -- still a win,
                # scored at the best target reached, not the later stop.
                status, closed = "won", True
                exit_idx, exit_price, exit_reason = level_idx, targets[level - 1], f"tp{level}"
            elif level >= 1:
                status, closed = "won", False  # still tracking for a higher target
                exit_idx, exit_price, exit_reason = level_idx, targets[level - 1], f"tp{level}"
            else:
                status, closed = "open", False
                exit_idx, exit_price, exit_reason = None, None, None

            if (status, closed, level) == (trade.status, trade.closed, trade.best_level):
                continue  # nothing actually changed since the last check

            r_multiple = pnl_pct = pnl_usd = None
            exit_time = None
            if exit_price is not None:
                sign = 1 if trade.direction == "long" else -1
                risk = abs(trade.entry_price - trade.stop_loss)
                pnl_pct = (exit_price - trade.entry_price) / trade.entry_price * 100 * sign
                r_multiple = ((exit_price - trade.entry_price) * sign) / risk if risk > 0 else 0.0
                qty = trade.position_usd / trade.entry_price  # margin x leverage, in units of the asset
                pnl_usd = qty * (exit_price - trade.entry_price) * sign
                exit_time = df["timestamp"].iloc[exit_idx].to_pydatetime()

            conn.execute(
                """UPDATE trades SET status=?, best_level=?, closed=?, exit_time=?, exit_price=?,
                   exit_reason=?, r_multiple=?, pnl_pct=?, pnl_usd=? WHERE id=?""",
                (status, level, int(closed), exit_time.isoformat() if exit_time else None,
                 exit_price, exit_reason, round(r_multiple, 2) if r_multiple is not None else None,
                 round(pnl_pct, 2) if pnl_pct is not None else None,
                 round(pnl_usd, 2) if pnl_usd is not None else None, trade.id),
            )
            trade.status, trade.best_level, trade.closed = status, level, closed
            trade.exit_time, trade.exit_price, trade.exit_reason = exit_time, exit_price, exit_reason
            trade.r_multiple = round(r_multiple, 2) if r_multiple is not None else None
            trade.pnl_pct = round(pnl_pct, 2) if pnl_pct is not None else None
            trade.pnl_usd = round(pnl_usd, 2) if pnl_usd is not None else None
            changed.append(trade)
        conn.commit()
    return changed


def get_trades(market: str | None = None, timeframe: str | None = None, status: str | None = None,
                limit: int = 200) -> list[PaperTrade]:
    """Most recent paper trades, optionally filtered, newest entry first."""
    clauses, params = [], []
    if market is not None:
        clauses.append("market = ?")
        params.append(market)
    if timeframe is not None:
        clauses.append("timeframe = ?")
        params.append(timeframe)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with closing(_connect()) as conn:
        rows = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM trades {where} ORDER BY entry_time DESC LIMIT ?", (*params, limit)
        ).fetchall()
    return [_row_to_trade(r) for r in rows]


def get_trade(trade_id: int) -> PaperTrade | None:
    with closing(_connect()) as conn:
        row = conn.execute(f"SELECT {_SELECT_COLUMNS} FROM trades WHERE id = ?", (trade_id,)).fetchone()
    return _row_to_trade(row) if row else None


@dataclass
class PaperStats:
    total: int
    open: int
    wins: int
    losses: int
    win_rate: float  # of resolved (won or lost) trades only
    avg_r: float
    total_r: float
    total_pnl_usd: float


def get_stats(market: str | None = None, timeframe: str | None = None, tier: str | None = None) -> PaperStats:
    """Aggregate win rate / R / $ stats -- the live, ever-growing counterpart
    to a one-off backtest.run_backtest() call. A 'won' trade counts as a win
    immediately (even if still tracking for a higher target) since the
    outcome can only improve from there, never flip to a loss."""
    clauses, params = [], []
    if market is not None:
        clauses.append("market = ?")
        params.append(market)
    if timeframe is not None:
        clauses.append("timeframe = ?")
        params.append(timeframe)
    if tier is not None:
        clauses.append("tier = ?")
        params.append(tier)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with closing(_connect()) as conn:
        rows = conn.execute(f"SELECT status, r_multiple, pnl_usd FROM trades {where}", params).fetchall()
    total = len(rows)
    open_count = sum(1 for status, _, _ in rows if status == "open")
    resolved_r = [r for status, r, _ in rows if status in ("won", "lost") and r is not None]
    wins = sum(1 for status, _, _ in rows if status == "won")
    losses = sum(1 for status, _, _ in rows if status == "lost")
    win_rate = wins / (wins + losses) * 100 if (wins + losses) else 0.0
    avg_r = sum(resolved_r) / len(resolved_r) if resolved_r else 0.0
    total_pnl_usd = sum(p for _, _, p in rows if p is not None)
    return PaperStats(
        total=total, open=open_count, wins=wins, losses=losses,
        win_rate=round(win_rate, 1), avg_r=round(avg_r, 2), total_r=round(sum(resolved_r), 2),
        total_pnl_usd=round(total_pnl_usd, 2),
    )
