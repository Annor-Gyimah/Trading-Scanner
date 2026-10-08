"""Smart Money Concepts (SMC) analysis, scoped specifically to the XAU/USDT gold perpetual.

Codifies the same manual method used for the one-off gold writeup: a higher
timeframe (HTF) defines structural bias, liquidity pools (equal highs/lows),
and the active range's premium/discount midpoint; a lower timeframe (LTF)
liquidity sweep AT one of those HTF levels, with a reclaim close and real
volume, is the entry trigger.

Deliberately narrower than a generic SMC toolkit: no fair-value-gap or
synthetic order-block detection. Every piece here is one that was checked
against real data first (see the Oct 2 sweep-and-reject reversal, and the
Oct 5 15:15 sweep-and-reclaim) rather than a textbook concept bolted on
without verification.

Not part of the crypto-universe breakout/pullback/approaching tiers. This is
intentionally tied to one instrument and run only via --gold-smc.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class LiquidityPool:
    kind: str  # "high" or "low"
    level: float
    touches: int


@dataclass
class GoldSMCSignal:
    direction: str  # "long" or "short"
    entry: float
    stop_loss: float
    targets: list[float]
    htf_bias: str  # "bullish", "bearish", "ranging"
    aligned_with_bias: bool
    zone_level: float
    zone_kind: str
    premium_discount: str  # "premium" or "discount"
    volume_ratio: float
    reason: str


def _find_swings(df: pd.DataFrame, left: int, right: int) -> tuple[list[int], list[int]]:
    """Fractal swing points: bar i is a swing high if its high is the max of the
    `left`+`right`-bar window centered on it (swing low mirrors on lows).
    Only returns swings with `right` confirmed bars after them -- no repaint."""
    highs = df["high"].values
    lows = df["low"].values
    n = len(df)
    swing_high_idx, swing_low_idx = [], []
    for i in range(left, n - right):
        window_h = highs[i - left : i + right + 1]
        if highs[i] == window_h.max() and np.argmax(window_h) == left:
            swing_high_idx.append(i)
        window_l = lows[i - left : i + right + 1]
        if lows[i] == window_l.min() and np.argmin(window_l) == left:
            swing_low_idx.append(i)
    return swing_high_idx, swing_low_idx


def _cluster_pools(df: pd.DataFrame, idx_list: list[int], kind: str, tolerance_pct: float) -> list[LiquidityPool]:
    """Group swing points within `tolerance_pct` of each other into liquidity pools
    (the "equal highs" / "equal lows" where stops and breakout orders cluster)."""
    if not idx_list:
        return []
    col = "high" if kind == "high" else "low"
    points = sorted(float(df[col].iloc[i]) for i in idx_list)
    pools = []
    cluster = [points[0]]
    for p in points[1:]:
        if abs(p - cluster[-1]) / cluster[-1] * 100 <= tolerance_pct:
            cluster.append(p)
        else:
            pools.append(LiquidityPool(kind=kind, level=float(np.mean(cluster)), touches=len(cluster)))
            cluster = [p]
    pools.append(LiquidityPool(kind=kind, level=float(np.mean(cluster)), touches=len(cluster)))
    return pools


def _classify_bias(df: pd.DataFrame, swing_high_idx: list[int], swing_low_idx: list[int]) -> str:
    """Bullish: last two confirmed swing highs AND lows both rising (HH + HL).
    Bearish: both falling (LH + LL). Anything else: ranging/mixed."""
    if len(swing_high_idx) < 2 or len(swing_low_idx) < 2:
        return "ranging"
    highs = [df["high"].iloc[i] for i in swing_high_idx[-2:]]
    lows = [df["low"].iloc[i] for i in swing_low_idx[-2:]]
    if highs[1] > highs[0] and lows[1] > lows[0]:
        return "bullish"
    if highs[1] < highs[0] and lows[1] < lows[0]:
        return "bearish"
    return "ranging"


def analyze_htf(
    df: pd.DataFrame,
    swing_left: int = 3,
    swing_right: int = 3,
    pool_tolerance_pct: float = 0.15,
    range_lookback_bars: int = 50,
) -> dict:
    """Bias, liquidity pools, and the active range's premium/discount midpoint, from the HTF.

    Bias uses the full available history (more swings to compare = a steadier
    read). The active range deliberately does NOT use the same full history:
    it takes the HIGHEST swing high and LOWEST swing low within only the last
    `range_lookback_bars` closed bars (default 30, ~5 days on 4h). Using the
    full history here would let old, structurally irrelevant swings (e.g. a
    high from a week before the last BOS) define "the range" -- that's not
    what a human reading the chart means by the active range, which is the
    current leg since the last major structure shift.
    """
    closed = df.iloc[:-1]
    sh_idx, sl_idx = _find_swings(closed, swing_left, swing_right)
    bias = _classify_bias(closed, sh_idx, sl_idx)
    high_pools = _cluster_pools(closed, sh_idx, "high", pool_tolerance_pct)
    low_pools = _cluster_pools(closed, sl_idx, "low", pool_tolerance_pct)

    recent_cutoff = len(closed) - range_lookback_bars
    recent_sh = [i for i in sh_idx if i >= recent_cutoff]
    recent_sl = [i for i in sl_idx if i >= recent_cutoff]
    range_high = float(closed["high"].iloc[recent_sh].max()) if recent_sh else float(closed["high"].iloc[-range_lookback_bars:].max())
    range_low = float(closed["low"].iloc[recent_sl].min()) if recent_sl else float(closed["low"].iloc[-range_lookback_bars:].min())
    if range_high < range_low:
        range_high, range_low = range_low, range_high
    equilibrium = (range_high + range_low) / 2

    return {
        "bias": bias,
        "high_pools": high_pools,
        "low_pools": low_pools,
        "range_high": range_high,
        "range_low": range_low,
        "equilibrium": equilibrium,
    }


def _nearest_pool(pools: list[LiquidityPool], price: float, max_distance_pct: float) -> LiquidityPool | None:
    if not pools:
        return None
    best = min(pools, key=lambda p: abs(p.level - price))
    if abs(best.level - price) / price * 100 > max_distance_pct:
        return None
    return best


def evaluate_gold_smc(
    htf_df: pd.DataFrame,
    ltf_df: pd.DataFrame,
    swing_left: int = 3,
    swing_right: int = 3,
    pool_tolerance_pct: float = 0.15,
    sweep_proximity_pct: float = 0.5,
    min_reject_strength: float = 0.5,
    sweep_vol_multiplier: float = 1.3,
) -> GoldSMCSignal | None:
    """Return a GoldSMCSignal if the latest closed LTF candle swept an HTF liquidity
    level (equal highs/lows or the active range extreme) and reclaimed it with a
    real-volume close, else None.

    direction="long": wicked below a support-type HTF level, closed back above it,
      green candle, strong close, real volume. Scored against the HTF bias --
      flagged as aligned if bias isn't bearish, countering it otherwise (still
      returned either way; a counter-trend bounce is a real, lower-conviction
      setup, not a non-setup).
    direction="short": the mirror image at a resistance-type HTF level.
    """
    htf = analyze_htf(htf_df, swing_left, swing_right, pool_tolerance_pct)

    ltf_closed = ltf_df.iloc[:-1]
    if len(ltf_closed) < 25:
        return None
    latest = ltf_closed.iloc[-1]
    prior = ltf_closed.iloc[:-1]

    candle_range = latest["high"] - latest["low"]
    if candle_range <= 0:
        return None
    avg_volume = prior["volume"].iloc[-20:].mean()
    volume_ratio = latest["volume"] / avg_volume if avg_volume > 0 else 0.0
    atr = (prior["high"] - prior["low"]).iloc[-14:].mean()

    price_now = float(latest["close"])
    premium_discount = "discount" if price_now < htf["equilibrium"] else "premium"

    support_candidates = htf["low_pools"] + [LiquidityPool("low", htf["range_low"], 1)]
    resistance_candidates = htf["high_pools"] + [LiquidityPool("high", htf["range_high"], 1)]

    # Bullish sweep: wick below a support level, close back above it, strong green close.
    bull_close_position = (latest["close"] - latest["low"]) / candle_range
    pool = _nearest_pool(support_candidates, float(latest["low"]), sweep_proximity_pct)
    if (
        pool is not None
        and latest["low"] < pool.level
        and latest["close"] > pool.level
        and bull_close_position >= min_reject_strength
        and latest["close"] > latest["open"]
        and volume_ratio >= sweep_vol_multiplier
    ):
        stop_loss = float(latest["low"] - 0.5 * atr)
        targets = [round(htf["equilibrium"], 2), round(htf["range_high"], 2)]
        aligned = htf["bias"] != "bearish"
        reason = (
            f"Swept {'equal lows' if pool.touches > 1 else 'range low'} at ${pool.level:.2f} "
            f"(touched {pool.touches}x) and reclaimed on {volume_ratio:.1f}x volume, in {premium_discount}. "
            f"HTF bias is {htf['bias']} -- this {'aligns with' if aligned else 'counters'} it."
        )
        return GoldSMCSignal(
            direction="long", entry=price_now, stop_loss=stop_loss, targets=targets,
            htf_bias=htf["bias"], aligned_with_bias=aligned, zone_level=round(pool.level, 2),
            zone_kind="htf_equal_lows" if pool.touches > 1 else "htf_range_low",
            premium_discount=premium_discount, volume_ratio=round(float(volume_ratio), 2), reason=reason,
        )

    # Bearish sweep: wick above a resistance level, close back below it, strong red close.
    bear_close_position = (latest["high"] - latest["close"]) / candle_range
    pool = _nearest_pool(resistance_candidates, float(latest["high"]), sweep_proximity_pct)
    if (
        pool is not None
        and latest["high"] > pool.level
        and latest["close"] < pool.level
        and bear_close_position >= min_reject_strength
        and latest["close"] < latest["open"]
        and volume_ratio >= sweep_vol_multiplier
    ):
        stop_loss = float(latest["high"] + 0.5 * atr)
        targets = [round(htf["equilibrium"], 2), round(htf["range_low"], 2)]
        aligned = htf["bias"] != "bullish"
        reason = (
            f"Swept {'equal highs' if pool.touches > 1 else 'range high'} at ${pool.level:.2f} "
            f"(touched {pool.touches}x) and rejected on {volume_ratio:.1f}x volume, in {premium_discount}. "
            f"HTF bias is {htf['bias']} -- this {'aligns with' if aligned else 'counters'} it."
        )
        return GoldSMCSignal(
            direction="short", entry=price_now, stop_loss=stop_loss, targets=targets,
            htf_bias=htf["bias"], aligned_with_bias=aligned, zone_level=round(pool.level, 2),
            zone_kind="htf_equal_highs" if pool.touches > 1 else "htf_range_high",
            premium_discount=premium_discount, volume_ratio=round(float(volume_ratio), 2), reason=reason,
        )

    return None
