"""Accumulation / basing detection -- the "quiet before the move" tier.

Flags symbols sitting in a long, tight consolidation that HASN'T broken out
yet, so they can be watchlisted ahead of evaluate_breakout() actually
triggering. Calibrated against two real examples (EDU/USDT and ORCA/USDT,
Sept-Oct 2026) before picking defaults: both based in a 16-29% range over a
~10-day (60-bar on 4h) window with ~2.5% ATR per bar and flat (not declining)
volume, before a 5-9x volume breakout candle. The defaults below come from
that calibration, not a guess.

Deliberately separate from evaluate_breakout(): this tier is for "not broken
out yet, worth watching"; evaluate_breakout() is for "broke out now, here's
the entry." A symbol should move from showing up here to showing up there,
not both at once -- this explicitly excludes anything that already looks
like it broke away from its own base.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class AccumulationSignal:
    symbol: str
    score: float
    base_high: float
    base_low: float
    base_range_pct: float
    atr_pct: float
    position_in_base: float  # 0 = sitting at the base low, 1 = sitting at the base high
    close: float
    reason: str


def evaluate_accumulation(
    df: pd.DataFrame,
    base_window: int = 60,
    max_range_pct: float = 30.0,
    max_atr_pct: float = 3.5,
    max_spike_ratio: float = 3.0,
    breakout_buffer_pct: float = 5.0,
) -> AccumulationSignal | None:
    """Return an AccumulationSignal if the last `base_window` closed bars form a tight,
    not-yet-broken base, else None.

    Requires ALL of:
      - the base's high-to-low range is <= `max_range_pct` -- tight enough to
        be coiling, not trending
      - average true range per bar inside the base is <= `max_atr_pct` --
        low realized volatility, the "quiet" half of quiet-then-loud
      - none of the last 3 bars has already spiked >= `max_spike_ratio` x the
        base's own average volume -- checked only on the most recent bars,
        not the whole window: a single noisy bar from weeks ago inside an
        otherwise quiet base is normal and shouldn't disqualify it, but a
        spike happening right now means it's already launching
      - the latest closed price is still within `breakout_buffer_pct` of the
        base's high and low -- i.e. still inside (or barely outside) the
        base, not already gapped away from it. Once that happens, this is
        evaluate_breakout()'s job, not this tier's.
    """
    closed = df.iloc[:-1]
    if len(closed) < base_window:
        return None
    base = closed.iloc[-base_window:]
    latest = closed.iloc[-1]

    base_high = float(base["high"].max())
    base_low = float(base["low"].min())
    if base_low <= 0 or base_high <= base_low:
        return None

    base_range_pct = (base_high - base_low) / base_low * 100
    if base_range_pct > max_range_pct:
        return None

    atr_pct = float(((base["high"] - base["low"]) / base["close"]).mean() * 100)
    if atr_pct > max_atr_pct:
        return None

    avg_volume = base["volume"].mean()
    if avg_volume > 0 and (base["volume"].iloc[-3:] / avg_volume).max() > max_spike_ratio:
        return None  # already launching right now, not quiet anymore

    close = float(latest["close"])
    if close > base_high * (1 + breakout_buffer_pct / 100):
        return None  # already broke away upward -- evaluate_breakout's territory now
    if close < base_low * (1 - breakout_buffer_pct / 100):
        return None  # already broke away downward

    position_in_base = (close - base_low) / (base_high - base_low)

    # Tighter range and lower volatility both score higher -- the "coiled
    # spring" reads as more imminent than a loose, choppy base.
    score = (max_range_pct - base_range_pct) + (max_atr_pct - atr_pct) * 3

    reason = (
        f"Basing in a {base_range_pct:.0f}% range (${base_low:.6g}-${base_high:.6g}) over the last "
        f"{base_window} bars, {atr_pct:.1f}% ATR/bar -- tight and quiet, hasn't broken out yet."
    )

    return AccumulationSignal(
        symbol="",
        score=round(score, 3),
        base_high=base_high,
        base_low=base_low,
        base_range_pct=round(base_range_pct, 1),
        atr_pct=round(atr_pct, 2),
        position_in_base=round(position_in_base, 2),
        close=close,
        reason=reason,
    )


@dataclass
class VolSqueezeSignal:
    """Self-referential sibling of AccumulationSignal: instead of a fixed
    absolute range%/ATR% cap, flags a symbol only when its CURRENT base is
    tighter than `percentile_threshold`% of its OWN trailing history.

    Built after backtesting evaluate_accumulation()'s fixed thresholds
    across 70 liquid symbols: it fired on 85-95% of all bars for
    BTC/BNB/TRX/SOL (tight-trading blue chips are "basing" by that
    definition almost always, so the flag carries no information for
    them) and 0% for several naturally volatile small-caps (never tight
    enough to qualify even in their own quietest moments) -- and its
    catch rate on genuinely idiosyncratic single-symbol explosions
    (34.5%) was WORSE than its own 52.4% overall base rate, meaning it
    was net-negatively associated with an imminent idiosyncratic move,
    not predictive of one. A relative, per-symbol threshold is the fix:
    "tight for THIS symbol, right now" auto-calibrates away the blue-chip/
    small-cap mismatch that broke the fixed-threshold version.
    """

    symbol: str
    score: float
    base_range_pct: float
    atr_pct: float
    range_percentile: float  # 0-100, current range% vs this symbol's own trailing history (lower = tighter than usual)
    atr_percentile: float
    position_in_base: float
    close: float
    reason: str


def evaluate_accumulation_relative(
    df: pd.DataFrame,
    base_window: int = 60,
    history_window: int = 300,
    percentile_threshold: float = 25.0,
    max_spike_ratio: float = 3.0,
    breakout_buffer_pct: float = 5.0,
) -> VolSqueezeSignal | None:
    """Return a VolSqueezeSignal if the current base is tighter than
    `percentile_threshold`% of this symbol's own trailing `history_window`
    bars (on BOTH range% and ATR%), and hasn't already broken away, else
    None. See VolSqueezeSignal's docstring for why this replaces a fixed
    absolute cap rather than just tuning the old one's numbers.
    """
    closed = df.iloc[:-1]
    if len(closed) < base_window + history_window:
        return None

    high_roll = closed["high"].rolling(base_window).max()
    low_roll = closed["low"].rolling(base_window).min()
    range_pct_series = (high_roll - low_roll) / low_roll * 100
    atr_pct_series = ((closed["high"] - closed["low"]) / closed["close"]).rolling(base_window).mean() * 100

    range_hist = range_pct_series.iloc[-history_window:]
    atr_hist = atr_pct_series.iloc[-history_window:]
    if range_hist.isna().any() or atr_hist.isna().any():
        return None

    current_range_pct = float(range_hist.iloc[-1])
    current_atr_pct = float(atr_hist.iloc[-1])
    range_percentile = float((range_hist < current_range_pct).mean() * 100)
    atr_percentile = float((atr_hist < current_atr_pct).mean() * 100)

    if range_percentile > percentile_threshold or atr_percentile > percentile_threshold:
        return None

    base = closed.iloc[-base_window:]
    base_high = float(base["high"].max())
    base_low = float(base["low"].min())
    if base_low <= 0 or base_high <= base_low:
        return None
    latest = closed.iloc[-1]

    avg_volume = base["volume"].mean()
    if avg_volume > 0 and (base["volume"].iloc[-3:] / avg_volume).max() > max_spike_ratio:
        return None  # already launching right now

    close = float(latest["close"])
    if close > base_high * (1 + breakout_buffer_pct / 100):
        return None
    if close < base_low * (1 - breakout_buffer_pct / 100):
        return None

    position_in_base = (close - base_low) / (base_high - base_low)
    score = (percentile_threshold - range_percentile) + (percentile_threshold - atr_percentile) * 1.5

    reason = (
        f"Basing in a {current_range_pct:.0f}% range -- tighter than {100 - range_percentile:.0f}% of this "
        f"symbol's own last {history_window} bars (ATR tighter than {100 - atr_percentile:.0f}% of its own history)."
    )

    return VolSqueezeSignal(
        symbol="", score=round(score, 3), base_range_pct=round(current_range_pct, 1),
        atr_pct=round(current_atr_pct, 2), range_percentile=round(range_percentile, 1),
        atr_percentile=round(atr_percentile, 1), position_in_base=round(position_in_base, 2),
        close=close, reason=reason,
    )
