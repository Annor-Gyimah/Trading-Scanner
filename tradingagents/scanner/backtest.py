"""Backtest the breakout/pullback detectors against real history.

Reuses evaluate_breakout()/evaluate_pullback() exactly as the live scanner
calls them -- this walks a long history bar-by-bar, at each step handing the
detector only the data that would have been available at that point (never
the future), so there's no separate "backtest version" of the logic that
could drift from what actually runs live.

Simplified to a single target per trade (TP1, or PullbackSignal's first
target) rather than the full staged TP1/TP2/TP3 ladder -- partial-exit
accounting across multiple targets adds real complexity for a result that's
still just an approximation (intrabar ordering of stop-vs-target is unknowable
from OHLC alone; this always checks stop first on an ambiguous candle, i.e.
the conservative assumption). Good for "does this detector have real edge on
this symbol/timeframe," not for simulating exact P&L.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .binance_data import fetch_ohlcv_history_df
from .breakout import evaluate_breakout, evaluate_pullback
from .smc import evaluate_smc_structure, evaluate_zone_retest
from .stock_data import fetch_stock_ohlcv_history_df

_STRATEGIES = {
    "breakout": evaluate_breakout, "pullback": evaluate_pullback,
    "smc": evaluate_smc_structure, "zone_retest": evaluate_zone_retest,
}

# Below this many bars, neither detector has enough history to fire anyway
# (they self-guard on length); just a cheap floor to skip pointless calls.
_MIN_WINDOW = 60


@dataclass
class BacktestTrade:
    direction: str  # "long" or "short"
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: pd.Timestamp
    exit_price: float
    exit_reason: str  # "target" | "stop" | "open_at_end" (data ran out mid-trade)
    r_multiple: float  # reward:risk actually realized; -1.0 on a clean stop-out
    pnl_pct: float


@dataclass
class BacktestSummary:
    symbol: str
    timeframe: str
    strategy: str
    bars_tested: int
    trades: list[BacktestTrade]

    @property
    def total(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.r_multiple > 0)

    @property
    def losses(self) -> int:
        return self.total - self.wins

    @property
    def win_rate(self) -> float:
        return self.wins / self.total * 100 if self.total else 0.0

    @property
    def avg_r(self) -> float:
        return sum(t.r_multiple for t in self.trades) / self.total if self.total else 0.0

    @property
    def total_r(self) -> float:
        return sum(t.r_multiple for t in self.trades)


def is_valid_setup(direction: str, entry: float, stop: float, target: float) -> bool:
    """A trade's own stop/target have to be on the correct side of entry to
    be usable at all. Targets are computed from the structural breakout
    level, not from entry price -- on a strong momentum candle, TP1 can land
    behind where this would actually enter -- so this rejects that case
    rather than letting a nonsense "hit target immediately at a loss" trade
    through. Shared by the backtester and the live paper-trading tracker so
    both apply the identical sanity check.
    """
    if direction == "long":
        return stop < entry < target
    return target < entry < stop


def walk_for_exit(
    df: pd.DataFrame, start_idx: int, direction: str, stop: float, target: float
) -> tuple[int, float, str] | None:
    """Scan `df` from `start_idx` (inclusive) forward for whichever of
    stop/target is touched first. Stop is checked before target on any single
    ambiguous candle (the conservative assumption -- intrabar ordering isn't
    knowable from OHLC alone). Returns (exit_row_index, exit_price,
    "stop"|"target"), or None if neither has been hit within the given data
    (still open as of the last row).

    Shared by the backtester (walks a full historical slice in one call) and
    the live paper-trading tracker (re-run on fresh data each refresh, same
    rule, so a trade resolves identically whichever one checks it).
    """
    for idx in range(start_idx, len(df)):
        row = df.iloc[idx]
        if direction == "long":
            if row["low"] <= stop:
                return idx, stop, "stop"
            if row["high"] >= target:
                return idx, target, "target"
        else:
            if row["high"] >= stop:
                return idx, stop, "stop"
            if row["low"] <= target:
                return idx, target, "target"
    return None


def trade_outcome(direction: str, entry_price: float, stop: float, exit_price: float) -> tuple[float, float]:
    """(r_multiple, pnl_pct) for one exit -- shared math so a backtest trade
    and a paper trade are scored identically."""
    sign = 1 if direction == "long" else -1
    risk = abs(entry_price - stop)
    pnl_pct = (exit_price - entry_price) / entry_price * 100 * sign
    r_multiple = ((exit_price - entry_price) * sign) / risk if risk > 0 else 0.0
    return r_multiple, pnl_pct


def run_backtest(
    symbol: str,
    timeframe: str = "1h",
    strategy: str = "breakout",
    market: str = "crypto",
    total_bars: int = 1000,
) -> BacktestSummary:
    """Replay `strategy` ("breakout" or "pullback") over `total_bars` of real
    history for `symbol`, one bar at a time, exactly as the live scanner
    would have seen it. Returns every simulated trade plus summary stats.
    """
    if strategy not in _STRATEGIES:
        raise ValueError(f"Unknown strategy {strategy!r}; choose from {list(_STRATEGIES)}")
    evaluate_fn = _STRATEGIES[strategy]

    if market == "stocks-real":
        df = fetch_stock_ohlcv_history_df(symbol, timeframe=timeframe, total_bars=total_bars)
    else:
        df = fetch_ohlcv_history_df(symbol, timeframe=timeframe, total_bars=total_bars)

    n = len(df)
    trades: list[BacktestTrade] = []
    k = _MIN_WINDOW

    while k < n - 1:
        # df.iloc[:k+2]'s last row is "forming" (excluded by the detector), so
        # the "latest closed" candle it evaluates is row k -- the real entry
        # trigger. Everything from row k+1 onward is genuinely future data the
        # detector never saw, so no lookahead here.
        signal = evaluate_fn(df.iloc[: k + 2])
        if signal is None:
            k += 1
            continue

        if strategy == "breakout":
            target = signal.take_profits[0]
        elif strategy == "pullback":
            target = signal.targets[0]
        else:
            target = signal.target  # "smc" or "zone_retest"
        entry = signal.close
        if not is_valid_setup(signal.direction, entry, signal.stop_loss, target):
            k += 1
            continue

        entry_time = df["timestamp"].iloc[k]
        result = walk_for_exit(df, k + 1, signal.direction, signal.stop_loss, target)
        if result is not None:
            exit_idx, exit_price, exit_reason = result
            r_multiple, pnl_pct = trade_outcome(signal.direction, entry, signal.stop_loss, exit_price)
            trades.append(BacktestTrade(
                direction=signal.direction, entry_time=entry_time, entry_price=round(entry, 6),
                exit_time=df["timestamp"].iloc[exit_idx], exit_price=round(exit_price, 6),
                exit_reason=exit_reason, r_multiple=round(r_multiple, 2), pnl_pct=round(pnl_pct, 2),
            ))
            k = exit_idx + 1  # resume scanning for the next signal right after this trade closes
        else:
            # Still open as of the last available candle -- mark-to-market and stop,
            # there's no more data to resolve it or find a next entry.
            last_close = float(df["close"].iloc[-1])
            r_multiple, pnl_pct = trade_outcome(signal.direction, entry, signal.stop_loss, last_close)
            trades.append(BacktestTrade(
                direction=signal.direction, entry_time=entry_time, entry_price=round(entry, 6),
                exit_time=df["timestamp"].iloc[-1], exit_price=round(last_close, 6),
                exit_reason="open_at_end", r_multiple=round(r_multiple, 2), pnl_pct=round(pnl_pct, 2),
            ))
            break

    return BacktestSummary(symbol=symbol, timeframe=timeframe, strategy=strategy, bars_tested=n, trades=trades)
