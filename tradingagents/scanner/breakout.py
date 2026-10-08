"""Early-stage breakout detection, in BOTH directions.

Scores a symbol's latest closed candle for the "range compression -> volume
expansion -> breakout" shape of a genuine, catchable momentum move -- as
opposed to an already-vertical, already-blown-off candle (the shape a
pump-and-dump call gets posted on top of, after the fact). See the
`max_candle_change_atr_mult` and RSI-band checks below for where that
distinction is enforced.

direction="long" breaks above the prior lookback-bar high; direction="short"
is the exact mirror, breaking below the prior lookback-bar low. Both share
every other gate (volume, blown-off cap, compression bonus) -- only the
level, the close-position requirement, and the RSI band direction flip. A
single candle can never qualify as both: a strong bullish close and a strong
bearish close are complementary (close_position + bear_close_position == 1),
so at most one direction's close-position gate can pass.

This is a technical screener, not a predictor. It flags candidates for
further research; it does not claim to know what happens next.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd


@dataclass
class BreakoutSignal:
    symbol: str
    direction: str  # "long" (broke above the lookback-bar high) or "short" (broke below the low)
    score: float
    close: float
    breakout_level: float
    stop_loss: float
    target: float
    take_profits: list[float]
    volume_ratio: float
    rsi: float
    candle_change_pct: float
    reason: str
    market_cap: float | None = None
    float_ratio: float | None = None  # circulating_supply / max_supply, context only


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def evaluate_breakout(
    df: pd.DataFrame,
    lookback: int = 20,
    compression_window: int = 20,
    volume_multiplier: float = 2.0,
    max_candle_change_atr_mult: float = 4.0,
    rsi_floor: float = 40.0,
    rsi_ceiling: float = 82.0,
) -> BreakoutSignal | None:
    """Return a BreakoutSignal (direction long or short) if the latest CLOSED candle
    is an early-stage breakout in either direction, else None.

    Hard requirements shared by both directions (these are what actually
    distinguish an early, catchable move from chasing one already blown off):
      - volume >= `volume_multiplier` x the trailing average (real
        participation, not a low-volume fakeout)
      - single-candle % change below `max_candle_change_atr_mult` x this
        symbol's own recent typical candle range -- excludes the "already up
        [or down] 60% this candle" shape; that move is over by the time you
        can see it, not starting. Scaled by the symbol's own ATR% rather than
        a flat percentage, because what counts as "blown off" is completely
        different on a 15m candle than a 1d candle, and different for BTC
        than for a volatile microcap -- a flat cutoff silently rejects most
        genuine daily breakouts (healthy daily alt moves routinely exceed a
        flat 12%) while barely doing anything on 15m candles.

    direction="long" additionally requires:
      - close breaks the prior `lookback`-bar high
      - closes in the upper 60% of its own candle range (a strong close, not
        a long-wick spike already reversing)
      - RSI *as of the candle before the breakout* within [rsi_floor,
        rsi_ceiling] -- rules out a move already deep in blow-off territory
        before this candle even printed. (Deliberately not measured on the
        breakout candle itself: RSI mechanically spikes toward 100 on the
        first directional candle after any tight compression, since the
        preceding average loss is near zero -- that would flag the
        healthiest setups as "overbought" for the wrong reason.)

    direction="short" is the exact mirror: breaks the prior `lookback`-bar
    low, closes in the lower 40% of its own range (equivalently, closes in
    the upper 60% of the range measured from the high -- the bearish version
    of "a strong close"), and pre-breakdown RSI within the mirrored band
    [100-rsi_ceiling, 100-rsi_floor] -- not already deeply oversold going in.

    A single candle can never qualify as both directions: close_position and
    its mirror (1 - close_position) can't both clear 0.6 at once.

    Range compression in the bars just before the move (tightening before
    the break, the textbook squeeze-then-release shape) is real but rare at
    any single snapshot, so it is rewarded in the score rather than
    required -- a breakout without prior compression still qualifies, it
    just ranks lower than one that had it.
    """
    if len(df) < max(lookback, compression_window) + 20:
        return None

    closed = df.iloc[:-1]  # exclude the still-forming candle
    latest = closed.iloc[-1]
    prior = closed.iloc[:-1]

    candle_range = latest["high"] - latest["low"]
    if candle_range <= 0:
        return None

    avg_volume = prior["volume"].iloc[-lookback:].mean()
    if avg_volume <= 0:
        return None
    volume_ratio = latest["volume"] / avg_volume
    if volume_ratio < volume_multiplier:
        return None  # neither direction can qualify without real participation

    true_range_pct = (prior["high"] - prior["low"]) / prior["close"] * 100
    pre_breakout_window = true_range_pct.iloc[-compression_window - 1 : -1]
    baseline_range = pre_breakout_window.mean() if not pre_breakout_window.empty else np.nan
    recent_compression = pre_breakout_window.iloc[-5:].mean() if not pre_breakout_window.empty else np.nan

    candle_change_pct = (latest["close"] - latest["open"]) / latest["open"] * 100
    # Floor the baseline at 1% so an unusually dead/illiquid pre-breakout
    # window (baseline_range near 0) can't make the cap absurdly tight.
    effective_baseline = baseline_range if pd.notna(baseline_range) and baseline_range > 1.0 else 1.0
    max_candle_change_pct = max_candle_change_atr_mult * effective_baseline
    if abs(candle_change_pct) > max_candle_change_pct:
        return None  # already blown off, in either direction

    rsi_series = _rsi(prior["close"])
    rsi = rsi_series.iloc[-1]
    if pd.isna(rsi):
        return None

    # Ratio < 1 means the range was tightening into the move (rewarded);
    # >= 1 (or data too short to tell) just earns no bonus, not a rejection.
    compression_ratio = (
        recent_compression / baseline_range
        if baseline_range and baseline_range > 0 and pd.notna(recent_compression)
        else 1.0
    )
    compression_bonus = max(0.0, 1 - compression_ratio)
    compression_note = (
        f"after {compression_bonus * 100:.0f}% range compression; " if compression_bonus > 0 else ""
    )
    atr = (prior["high"] - prior["low"]).iloc[-14:].mean()

    # ---- LONG: break above the prior lookback-bar high ----
    breakout_level = prior["high"].iloc[-lookback:].max()
    close_position = (latest["close"] - latest["low"]) / candle_range
    if latest["close"] > breakout_level and close_position >= 0.6 and rsi_floor <= rsi <= rsi_ceiling:
        stop_loss = breakout_level - 1.5 * atr
        range_height = breakout_level - prior["low"].iloc[-lookback:].min()
        target = breakout_level + range_height
        # Staged take-profits at fractions of the measured move, same
        # convention as a "TAKE PROFITS 1)...5)" signal-post ladder, but
        # derived from this symbol's own range instead of picked by eye.
        take_profits = [breakout_level + range_height * m for m in (0.5, 1.0, 1.618)]
        score = (
            volume_ratio * 2.0 + compression_bonus * 3.0 + close_position * 2.0
            + max(0, rsi_ceiling - rsi) * 0.05
        )
        reason = (
            f"Broke {lookback}-bar high (${breakout_level:.6g}) on {volume_ratio:.1f}x avg volume "
            f"{compression_note}pre-breakout RSI {rsi:.0f}, "
            f"strong close ({close_position * 100:.0f}% of candle range)."
        )
        return BreakoutSignal(
            symbol="", direction="long", score=round(score, 3), close=float(latest["close"]),
            breakout_level=float(breakout_level), stop_loss=float(stop_loss), target=float(target),
            take_profits=[round(float(tp), 6) for tp in take_profits],
            volume_ratio=round(float(volume_ratio), 2), rsi=round(float(rsi), 1),
            candle_change_pct=round(float(candle_change_pct), 2), reason=reason,
        )

    # ---- SHORT: break below the prior lookback-bar low (mirror) ----
    breakdown_level = prior["low"].iloc[-lookback:].min()
    bear_close_position = (latest["high"] - latest["close"]) / candle_range
    short_rsi_floor, short_rsi_ceiling = 100 - rsi_ceiling, 100 - rsi_floor
    if (
        latest["close"] < breakdown_level
        and bear_close_position >= 0.6
        and short_rsi_floor <= rsi <= short_rsi_ceiling
    ):
        stop_loss = breakdown_level + 1.5 * atr
        range_height = prior["high"].iloc[-lookback:].max() - breakdown_level
        target = breakdown_level - range_height
        take_profits = [breakdown_level - range_height * m for m in (0.5, 1.0, 1.618)]
        score = (
            volume_ratio * 2.0 + compression_bonus * 3.0 + bear_close_position * 2.0
            + max(0, rsi - short_rsi_floor) * 0.05
        )
        reason = (
            f"Broke {lookback}-bar low (${breakdown_level:.6g}) on {volume_ratio:.1f}x avg volume "
            f"{compression_note}pre-breakdown RSI {rsi:.0f}, "
            f"weak close ({bear_close_position * 100:.0f}% toward the candle's low)."
        )
        return BreakoutSignal(
            symbol="", direction="short", score=round(score, 3), close=float(latest["close"]),
            breakout_level=float(breakdown_level), stop_loss=float(stop_loss), target=float(target),
            take_profits=[round(float(tp), 6) for tp in take_profits],
            volume_ratio=round(float(volume_ratio), 2), rsi=round(float(rsi), 1),
            candle_change_pct=round(float(candle_change_pct), 2), reason=reason,
        )

    return None


@dataclass
class ApproachingSignal:
    """An UNCONFIRMED watchlist entry: the live, still-forming candle is near a breakout
    OR a breakdown (see `direction`).

    Not a trade signal -- this can flicker and vanish before the candle
    closes, which is exactly why evaluate_breakout() excludes this same bar.
    Use it to know what to have pulled up on a chart, not what to enter.
    """

    symbol: str
    direction: str  # "long" (near/testing a breakout above) or "short" (near/testing a breakdown below)
    status: str  # "testing" (already past the level, unconfirmed) or "near" (not yet, within threshold)
    distance_pct: float  # % from the level; sign convention: negative means already past it
    breakout_level: float  # the level being approached -- a high for "long", a low for "short"
    close: float
    volume_pace_ratio: float  # projected full-bar volume / trailing average
    bar_elapsed_pct: float  # how far through the still-forming candle we are
    rsi: float
    reason: str


def evaluate_approaching(
    df: pd.DataFrame,
    timeframe_seconds: int,
    lookback: int = 20,
    approach_threshold_pct: float = 2.0,
    min_volume_pace_ratio: float = 1.3,
    rsi_floor: float = 40.0,
    rsi_ceiling: float = 82.0,
    min_bar_elapsed_pct: float = 0.15,
) -> ApproachingSignal | None:
    """Return an ApproachingSignal (direction long or short) if the LIVE, still-forming
    candle looks like it's building toward a breakout or a breakdown, else None.

    Uses the same levels and average volume as evaluate_breakout() (both
    computed from fully closed bars, so they can't repaint), but reads the
    current price/volume off the in-progress candle instead of waiting for
    it to close. Volume is normalized by how far through the bar we are
    (`current_volume / fraction_elapsed`) so a 1h candle 10 minutes old isn't
    penalized for not yet having a full hour's volume.

    Shared requirements for either direction:
      - projected full-bar volume pace >= `min_volume_pace_ratio` x the
        trailing average (participation is already building, not just price
        drifting on thin volume)
      - at least `min_bar_elapsed_pct` of the candle has printed -- a volume
        projection from the first few seconds of a bar is too noisy to mean
        anything.

    direction="long": price within `approach_threshold_pct` of the prior
      lookback-bar high (or already above it, unconfirmed -- status=
      "testing"), with RSI as of the last closed bar within [rsi_floor,
      rsi_ceiling] (not already deep in blow-off territory).
    direction="short": the exact mirror against the prior lookback-bar low,
      with RSI within the mirrored band [100-rsi_ceiling, 100-rsi_floor]
      (not already deeply oversold going in).
    """
    if len(df) < lookback + 20:
        return None

    forming = df.iloc[-1]
    closed = df.iloc[:-1]
    prior = closed  # all fully-closed bars are fair game as "prior" here

    bar_open = forming["timestamp"]
    if bar_open.tzinfo is None:
        bar_open = bar_open.tz_localize("UTC")
    elapsed_seconds = (datetime.now(timezone.utc) - bar_open.to_pydatetime()).total_seconds()
    bar_elapsed_pct = max(0.0, min(1.0, elapsed_seconds / timeframe_seconds))
    if bar_elapsed_pct < min_bar_elapsed_pct:
        return None

    avg_volume = prior["volume"].iloc[-lookback:].mean()
    if avg_volume <= 0:
        return None
    projected_volume = forming["volume"] / bar_elapsed_pct
    volume_pace_ratio = projected_volume / avg_volume
    if volume_pace_ratio < min_volume_pace_ratio:
        return None  # neither direction has real participation building yet

    rsi_series = _rsi(closed["close"])
    rsi = rsi_series.iloc[-1]
    if pd.isna(rsi):
        return None

    # ---- LONG: approaching/testing a breakout above ----
    breakout_level = prior["high"].iloc[-lookback:].max()
    if pd.notna(breakout_level) and breakout_level > 0 and rsi_floor <= rsi <= rsi_ceiling:
        distance_pct = (breakout_level - forming["close"]) / breakout_level * 100
        status = "testing" if distance_pct <= 0 else "near"
        if status == "testing" or distance_pct <= approach_threshold_pct:
            status_text = (
                f"already {abs(distance_pct):.1f}% above the {lookback}-bar high, unconfirmed"
                if status == "testing"
                else f"{distance_pct:.1f}% below the {lookback}-bar high"
            )
            reason = (
                f"{status_text}; volume pace {volume_pace_ratio:.1f}x avg "
                f"({bar_elapsed_pct * 100:.0f}% through the candle), RSI {rsi:.0f}."
            )
            return ApproachingSignal(
                symbol="", direction="long", status=status, distance_pct=round(float(distance_pct), 2),
                breakout_level=float(breakout_level), close=float(forming["close"]),
                volume_pace_ratio=round(float(volume_pace_ratio), 2),
                bar_elapsed_pct=round(float(bar_elapsed_pct) * 100, 0), rsi=round(float(rsi), 1), reason=reason,
            )

    # ---- SHORT: approaching/testing a breakdown below (mirror) ----
    breakdown_level = prior["low"].iloc[-lookback:].min()
    short_rsi_floor, short_rsi_ceiling = 100 - rsi_ceiling, 100 - rsi_floor
    if pd.notna(breakdown_level) and breakdown_level > 0 and short_rsi_floor <= rsi <= short_rsi_ceiling:
        distance_pct = (forming["close"] - breakdown_level) / breakdown_level * 100
        status = "testing" if distance_pct <= 0 else "near"
        if status == "testing" or distance_pct <= approach_threshold_pct:
            status_text = (
                f"already {abs(distance_pct):.1f}% below the {lookback}-bar low, unconfirmed"
                if status == "testing"
                else f"{distance_pct:.1f}% above the {lookback}-bar low"
            )
            reason = (
                f"{status_text}; volume pace {volume_pace_ratio:.1f}x avg "
                f"({bar_elapsed_pct * 100:.0f}% through the candle), RSI {rsi:.0f}."
            )
            return ApproachingSignal(
                symbol="", direction="short", status=status, distance_pct=round(float(distance_pct), 2),
                breakout_level=float(breakdown_level), close=float(forming["close"]),
                volume_pace_ratio=round(float(volume_pace_ratio), 2),
                bar_elapsed_pct=round(float(bar_elapsed_pct) * 100, 0), rsi=round(float(rsi), 1), reason=reason,
            )

    return None


@dataclass
class PullbackSignal:
    """A pullback following a recent strong impulse move, in EITHER direction.

    direction="long": price pulled back toward the broken level and is
      holding above it -- a continuation entry, same direction as the
      original breakout, at a better price than chasing the impulse candle.
    direction="short": price pulled back THROUGH the broken level and failed
      to hold it -- the level that should have acted as support gave way,
      which reads as the breakout failing rather than a healthy dip. This is
      counter-trend and needs real confirmation (a decisive close below the
      level on real volume), not just "it came back down a bit" -- a shallow
      dip in a strong trend is the normal case, not a reversal.
    """

    symbol: str
    direction: str  # "long" or "short"
    score: float
    close: float
    broken_level: float
    impulse_high: float
    retrace_pct: float  # % of the impulse's range retraced; >100 means price broke back below the level
    stop_loss: float
    targets: list[float]
    volume_ratio: float
    reason: str


def _find_recent_impulse(
    closed: pd.DataFrame,
    lookback: int,
    impulse_window: int,
    min_breakout_pct: float,
) -> tuple[float, float, int] | None:
    """Find the highest high in the last `impulse_window` bars and what it broke above.

    Returns (impulse_high, pre_impulse_level, bars_since_impulse), or None if
    no clearance above `min_breakout_pct` was found -- i.e. there was no real
    impulse to have pulled back from, not a candidate for either direction.
    """
    if len(closed) < impulse_window + lookback:
        return None

    recent = closed.iloc[-impulse_window:]
    impulse_pos = recent["high"].values.argmax()
    impulse_high = recent["high"].iloc[impulse_pos]
    bars_since_impulse = len(recent) - 1 - impulse_pos

    # A fixed block of `lookback` bars entirely BEFORE the impulse window --
    # deliberately not anchored to the exact peak bar, since a real impulse
    # usually climbs over several bars (not one single-bar spike), so bars
    # just before the peak are often already part of the rise itself. Using
    # the peak bar as the cutoff let those bars inflate the "pre-breakout"
    # level and killed the clearance check on real multi-bar breakouts.
    if len(closed) < impulse_window + lookback:
        return None
    pre_impulse_level = closed["high"].iloc[-(impulse_window + lookback) : -impulse_window].max()
    if pd.isna(pre_impulse_level) or pre_impulse_level <= 0:
        return None

    clearance_pct = (impulse_high - pre_impulse_level) / pre_impulse_level * 100
    if clearance_pct < min_breakout_pct:
        return None  # no real impulse, just normal chop

    return float(impulse_high), float(pre_impulse_level), bars_since_impulse


def evaluate_pullback(
    df: pd.DataFrame,
    lookback: int = 20,
    impulse_window: int = 15,
    min_breakout_pct: float = 3.0,
    min_retrace_pct: float = 1.5,
    breakdown_buffer_pct: float = 0.5,
    breakdown_vol_multiplier: float = 1.3,
) -> PullbackSignal | None:
    """Return a PullbackSignal (direction long or short) following a recent impulse, else None.

    Finds the highest high in the last `impulse_window` closed bars and the
    level it broke above (`_find_recent_impulse`); everything else is
    measured relative to that level and that high -- both fixed, historical
    reference points, so this can't repaint the way the approaching tier can.

    direction="long" requires:
      - some real pullback happened (>= min_retrace_pct of the impulse range)
      - the broken level is still holding: latest close > that level. This is
        the actual gate -- NOT a cap on how deep the retrace is allowed to be
        (an earlier version capped it at 70% of the impulse range, which
        rejects exactly the deep-but-still-holding retests that make the best
        entries on a shallow-clearance impulse, where even a modest pullback
        in price terms is a large % of a small range). Retrace depth is used
        for scoring, not as a reject.
      - the latest closed candle isn't still actively making a fresh low
        (green close, OR a higher low than the bar before it) -- some sign
        the dip is arresting, without demanding a full reversal candle

    direction="short" requires:
      - latest close has broken back BELOW the level by more than
        `breakdown_buffer_pct` -- a real close through it, not a wick
      - volume on that break >= `breakdown_vol_multiplier` x the trailing
        average -- real selling, not a low-volume drift
    This is deliberately a higher bar than the long side: a normal pullback
    dipping below a level briefly is common and does NOT mean the breakout
    failed, so the short side requires both a clean close through it and
    real participation before calling it a reversal.
    """
    if len(df) < lookback + impulse_window + 5:
        return None

    closed = df.iloc[:-1]
    impulse = _find_recent_impulse(closed, lookback, impulse_window, min_breakout_pct)
    if impulse is None:
        return None
    impulse_high, pre_impulse_level, bars_since_impulse = impulse
    if bars_since_impulse < 1:
        return None  # still forming the impulse itself, nothing to pull back from yet

    latest = closed.iloc[-1]
    prior = closed.iloc[:-1]
    impulse_range = impulse_high - pre_impulse_level
    if impulse_range <= 0:
        return None

    retrace_pct = (impulse_high - latest["close"]) / impulse_range * 100
    if retrace_pct < min_retrace_pct:
        return None  # still sitting at/near the high, nothing to react to yet

    atr = (prior["high"] - prior["low"]).iloc[-14:].mean()
    avg_volume = prior["volume"].iloc[-lookback:].mean()
    volume_ratio = latest["volume"] / avg_volume if avg_volume > 0 else 0.0

    if latest["close"] > pre_impulse_level:
        stabilizing = latest["close"] > latest["open"] or latest["low"] >= prior["low"].iloc[-1]
        if not stabilizing:
            return None
        stop_loss = pre_impulse_level - atr
        targets = [impulse_high, impulse_high + impulse_range * 0.618]
        # Reward deeper (but still-holding) retests -- they offer tighter
        # risk and better reward than catching it near the recent high.
        score = min(retrace_pct, 95.0) / 20 + (2.0 if latest["close"] > latest["open"] else 1.0)
        reason = (
            f"Pulled back {retrace_pct:.0f}% of the recent breakout range and is holding above "
            f"${pre_impulse_level:.6g} (broken resistance now acting as support); "
            f"latest candle {'bullish' if latest['close'] > latest['open'] else 'higher low than the bar before it'}."
        )
        return PullbackSignal(
            symbol="", direction="long", score=round(score, 3), close=float(latest["close"]),
            broken_level=float(pre_impulse_level), impulse_high=float(impulse_high),
            retrace_pct=round(float(retrace_pct), 1), stop_loss=float(stop_loss),
            targets=[round(float(t), 6) for t in targets], volume_ratio=round(float(volume_ratio), 2),
            reason=reason,
        )

    broke_below_pct = (pre_impulse_level - latest["close"]) / pre_impulse_level * 100
    if broke_below_pct > breakdown_buffer_pct and volume_ratio >= breakdown_vol_multiplier:
        stop_loss = pre_impulse_level + atr
        targets = [pre_impulse_level - impulse_range * 0.5, pre_impulse_level - impulse_range]
        score = volume_ratio * 2.0 + min(broke_below_pct, 10.0) * 0.5
        reason = (
            f"Broke back below ${pre_impulse_level:.6g} (the level the prior breakout cleared) by "
            f"{broke_below_pct:.1f}% on {volume_ratio:.1f}x avg volume -- the breakout failed to hold, "
            "not just a normal dip."
        )
        return PullbackSignal(
            symbol="", direction="short", score=round(score, 3), close=float(latest["close"]),
            broken_level=float(pre_impulse_level), impulse_high=float(impulse_high),
            retrace_pct=round(float(retrace_pct), 1), stop_loss=float(stop_loss),
            targets=[round(float(t), 6) for t in targets], volume_ratio=round(float(volume_ratio), 2),
            reason=reason,
        )

    return None
