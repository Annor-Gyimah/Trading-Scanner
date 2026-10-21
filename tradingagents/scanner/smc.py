"""Market-structure breaks (BOS/CHoCH) and liquidity-pool sweeps, adapted
from LuxAlgo's "Smart Money Concepts" and "Liquidity Swings" indicators
(see LUX_ALGO_SMC.pine / LUX_ALGO_LIQUIDITY_SWINGS.pine at the repo root).

Swing points are confirmed via a symmetric N-bar pivot (a bar's high/low is
the extreme of the N bars on either side of it) -- the same definition
Liquidity Swings uses (ta.pivothigh/pivotlow(length, length)), and
consistent with this scanner's own N-bar-lookback convention elsewhere
(breakout.py's lookback). SMC.pine's own leg-flip pivot method is a more
roundabout way to the same kind of swing point; the symmetric pivot is
simpler and plays nicer with this codebase's existing style.

A pivot can only be confirmed `length` bars after it happens (you need the
bars on both sides to know it was the extreme) -- like Pullback, this tier
is inherently lagging/hindsight-based, not live like Approaching's normal
breakout read.

BOS vs CHoCH, read directly from LUX_ALGO_SMC.pine's displayStructure():
    tag = CHOCH if trend.bias == BEARISH else BOS      (on a bullish break)
    tag = CHOCH if trend.bias == BULLISH else BOS       (on a bearish break)
i.e. CHoCH is the break that FLIPS the prevailing trend; BOS is a break
that CONTINUES it. "bars_held" (how long the broken level survived before
breaking) is exactly the length of the horizontal line the Pine script
draws for it -- scored as a strength multiplier per the idea that a level
tested/held longer has more participants anchored to it. That's a
reasonable, widely-repeated SMC heuristic, not something empirically
proven the way breakout.py's volume/RSI gates are -- treat the score as a
ranking aid, not a validated edge.

Equal-high/low liquidity pools (EQH/EQL in SMC.pine) feed in too: a broken
level that sits within an ATR-scaled band of the PRIOR same-type swing is
treated as sweeping an established pool (more resting liquidity, by the
Liquidity Swings indicator's own logic) rather than a lone level, and
scored higher for it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class SMCSignal:
    symbol: str
    direction: str  # "long" (bullish break, i.e. a swing HIGH got closed through) or "short" (mirror)
    structure: str  # "BOS" (continues the prevailing trend) or "CHoCH" (flips it)
    score: float
    close: float
    level: float  # the swing price that just got broken
    stop_loss: float
    target: float
    bars_held: int  # how many bars the broken level had stood before breaking
    touches: int  # how many times price re-entered the zone without closing through, before the break
    volume_ratio: float
    swept_pool: bool  # True if `level` sits within an ATR-scaled band of the prior same-type swing (EQH/EQL)
    reason: str
    entry_time: pd.Timestamp | None = None


@dataclass
class ApproachingSMCSignal:
    symbol: str
    direction: str  # "long" or "short"
    status: str  # "testing" (already past the level on the live candle, unconfirmed) or "near"
    would_be: str  # "BOS" or "CHoCH" if this holds through the close
    level: float
    distance_pct: float
    bars_held: int
    close: float
    reason: str


@dataclass
class ZoneRetestSignal:
    """Price re-entered a STILL-ACTIVE (never-swept) liquidity zone and
    closed back out the far side, holding it -- the "returned to the zone
    and shot up/down again" pattern (box-shaped zone, not a single price;
    see LUX_ALGO_LIQUIDITY_SWINGS.pine's area/box, "Wick Extremity" mode).
    direction="long" = bounced off a support (swing-low) zone; "short" =
    rejected off a resistance (swing-high) zone. Different from an
    SMCSignal: that tier fires on the break of a zone; this one fires on a
    zone HOLDING under a retest, no break involved."""

    symbol: str
    direction: str
    score: float
    close: float
    zone_top: float
    zone_bottom: float
    stop_loss: float
    target: float
    touches: int  # prior tests of this zone before this one
    volume_ratio: float
    reason: str
    entry_time: pd.Timestamp | None = None


def _find_confirmed_pivots(highs: np.ndarray, lows: np.ndarray, length: int):
    """Symmetric length-bar pivots -- a bar's high (low) is the max (min) of
    the `2*length+1`-bar window centered on it. Returns (pivot_highs,
    pivot_lows) as lists of (bar_index, price), ordered by bar_index. Only
    covers bars with `length` confirmed bars on both sides -- the most
    recent `length` bars can never produce a pivot yet."""
    n = len(highs)
    ph, pl = [], []
    for i in range(length, n - length):
        window_h = highs[i - length : i + length + 1]
        if highs[i] >= window_h.max():
            ph.append((i, float(highs[i])))
        window_l = lows[i - length : i + length + 1]
        if lows[i] <= window_l.min():
            pl.append((i, float(lows[i])))
    return ph, pl


def _is_liquidity_pool(same_type_pivots: list[tuple[int, float]], pivot_bar: int, level: float, atr: float) -> bool:
    """True if `level` sits within an ATR-scaled band of the PRIOR same-type
    pivot before it -- the EQH/EQL ("equal high/low") concept from
    SMC.pine: a repeated level is read as an established liquidity pool,
    not a one-off swing. Threshold mirrors SMC.pine's default (0.1 * ATR)."""
    if atr <= 0:
        return False
    prior = [p for b, p in same_type_pivots if b < pivot_bar]
    if not prior:
        return False
    return abs(level - prior[-1]) < 0.1 * atr


def _replay_structure(df: pd.DataFrame, length: int):
    """Replay the BOS/CHoCH state machine bar-by-bar over every CLOSED
    candle (excludes the still-forming one, same convention as
    evaluate_breakout) -- mirrors LUX_ALGO_SMC.pine's displayStructure().

    Returns (events, swing_high, swing_low, trend_bias, closed_df):
      - events: every break found, as dicts with bar/direction/structure/
        level/pivot_bar/touches/swept_pool.
      - swing_high/swing_low: the latest UNBROKEN pivot of each type as of
        the last closed bar (None if none yet, or if the only one found
        has already been crossed) -- what Approaching reads against.
      - trend_bias: +1 bullish, -1 bearish, 0 unknown, as of the last event.
    """
    closed = df.iloc[:-1].reset_index(drop=True)
    n = len(closed)
    if n < length * 2 + 5:
        return [], None, None, 0, closed

    highs = closed["high"].to_numpy()
    lows = closed["low"].to_numpy()
    closes = closed["close"].to_numpy()
    opens = closed["open"].to_numpy()
    atr = float((closed["high"] - closed["low"]).iloc[-14:].mean())

    ph, pl = _find_confirmed_pivots(highs, lows, length)
    ph_known_at = [(bar + length, bar, price) for bar, price in ph]
    pl_known_at = [(bar + length, bar, price) for bar, price in pl]

    # swing_high/swing_low dicts: level (the far/outer edge -- what BOS/CHoCH
    # crosses), zone_top/zone_bottom (the box: wick extremity to candle
    # body, "Wick Extremity" mode in LUX_ALGO_LIQUIDITY_SWINGS.pine), plus
    # pivot_bar/crossed/touches bookkeeping.
    swing_high = None
    swing_low = None
    trend_bias = 0
    events = []
    ph_idx = pl_idx = 0

    for bar in range(length, n):
        while ph_idx < len(ph_known_at) and ph_known_at[ph_idx][0] == bar:
            _, pivot_bar, price = ph_known_at[ph_idx]
            swing_high = {
                "level": price, "pivot_bar": pivot_bar, "crossed": False, "touches": 0,
                "zone_top": price, "zone_bottom": max(closes[pivot_bar], opens[pivot_bar]),
            }
            ph_idx += 1
        while pl_idx < len(pl_known_at) and pl_known_at[pl_idx][0] == bar:
            _, pivot_bar, price = pl_known_at[pl_idx]
            swing_low = {
                "level": price, "pivot_bar": pivot_bar, "crossed": False, "touches": 0,
                "zone_top": min(closes[pivot_bar], opens[pivot_bar]), "zone_bottom": price,
            }
            pl_idx += 1

        if swing_high is not None and not swing_high["crossed"]:
            if lows[bar] < swing_high["level"] and closes[bar] <= swing_high["level"]:
                swing_high["touches"] += 1
            if closes[bar] > swing_high["level"]:
                structure = "CHoCH" if trend_bias == -1 else "BOS"
                events.append({
                    "bar": bar, "direction": "long", "structure": structure,
                    "level": swing_high["level"], "pivot_bar": swing_high["pivot_bar"],
                    "bars_held": bar - swing_high["pivot_bar"], "touches": swing_high["touches"],
                    "swept_pool": _is_liquidity_pool(ph, swing_high["pivot_bar"], swing_high["level"], atr),
                })
                swing_high["crossed"] = True
                trend_bias = 1

        if swing_low is not None and not swing_low["crossed"]:
            if highs[bar] > swing_low["level"] and closes[bar] >= swing_low["level"]:
                swing_low["touches"] += 1
            if closes[bar] < swing_low["level"]:
                structure = "CHoCH" if trend_bias == 1 else "BOS"
                events.append({
                    "bar": bar, "direction": "short", "structure": structure,
                    "level": swing_low["level"], "pivot_bar": swing_low["pivot_bar"],
                    "bars_held": bar - swing_low["pivot_bar"], "touches": swing_low["touches"],
                    "swept_pool": _is_liquidity_pool(pl, swing_low["pivot_bar"], swing_low["level"], atr),
                })
                swing_low["crossed"] = True
                trend_bias = -1

    swing_high_open = swing_high if swing_high is not None and not swing_high["crossed"] else None
    swing_low_open = swing_low if swing_low is not None and not swing_low["crossed"] else None
    return events, swing_high_open, swing_low_open, trend_bias, closed


def _score_break(e: dict, volume_ratio: float) -> float:
    """Heuristic, not an empirically-tuned gate like breakout.py's: a CHoCH
    (trend-flip) is weighted as more significant news than a plain BOS
    (continuation); a longer-held level and more prior touches both proxy
    for more resting liquidity/participants, per the indicators this is
    adapted from. Capped bars_held term so an ancient, barely-relevant
    level doesn't dominate the score on its own."""
    return (
        volume_ratio * 2.0
        + min(e["bars_held"] / 20, 3.0)
        + (1.5 if e["structure"] == "CHoCH" else 0.5)
        + (1.0 if e["swept_pool"] else 0.0)
        + min(e["touches"], 5) * 0.2
    )


def evaluate_smc_structure(df: pd.DataFrame, length: int = 20, volume_lookback: int = 20) -> SMCSignal | None:
    """A BOS/CHoCH break on the latest CLOSED candle, in either direction, or
    None. Entirely separate detector from evaluate_breakout() -- a
    structure break and an N-bar-high/low breakout are different lenses on
    the same price action and can disagree."""
    events, _, _, _, closed = _replay_structure(df, length)
    if not events or len(closed) < volume_lookback + 1:
        return None
    last_bar = len(closed) - 1
    hits = [e for e in events if e["bar"] == last_bar]
    if not hits:
        return None
    e = hits[0]  # a bullish and bearish break can't both land on the same candle

    latest = closed.iloc[-1]
    prior = closed.iloc[:-1]
    avg_volume = prior["volume"].iloc[-volume_lookback:].mean()
    volume_ratio = float(latest["volume"] / avg_volume) if avg_volume > 0 else 0.0
    atr = float((prior["high"] - prior["low"]).iloc[-14:].mean())
    level = e["level"]

    if e["direction"] == "long":
        # invalidation anchor: the nearest prior swing low, if the replay found one before this break
        prior_lows = closed["low"].iloc[max(0, e["pivot_bar"] - length) : e["pivot_bar"]]
        anchor = float(prior_lows.min()) if not prior_lows.empty else level - atr * 3
        stop_loss = anchor - 0.5 * atr
        target = level + (level - stop_loss) * 2
    else:
        prior_highs = closed["high"].iloc[max(0, e["pivot_bar"] - length) : e["pivot_bar"]]
        anchor = float(prior_highs.max()) if not prior_highs.empty else level + atr * 3
        stop_loss = anchor + 0.5 * atr
        target = level - (stop_loss - level) * 2

    score = _score_break(e, volume_ratio)
    pool_note = "sweeping an equal-high/low liquidity pool" if e["swept_pool"] else "a standalone swing level"
    verb = "above" if e["direction"] == "long" else "below"
    reason = (
        f"{e['structure']} -- closed {verb} a swing level (${level:.6g}) that held for {e['bars_held']} bars "
        f"({e['touches']} prior touch(es)), {pool_note}, on {volume_ratio:.1f}x avg volume."
    )
    return SMCSignal(
        symbol="", direction=e["direction"], structure=e["structure"], score=round(score, 3),
        close=float(latest["close"]), level=round(level, 6), stop_loss=round(stop_loss, 6),
        target=round(target, 6), bars_held=e["bars_held"], touches=e["touches"],
        volume_ratio=round(volume_ratio, 2), swept_pool=e["swept_pool"], reason=reason,
        entry_time=latest["timestamp"],
    )


def evaluate_smc_approaching(
    df: pd.DataFrame, length: int = 20, approach_threshold_pct: float = 2.0,
) -> ApproachingSMCSignal | None:
    """Is the live, still-forming candle testing or nearing the nearest
    unbroken swing level? Same "watchlist, not a signal" framing as
    evaluate_approaching -- this can vanish if the candle closes back on
    the other side before the bar finishes. Returns whichever side (long
    or short) is CLOSER right now, since only one unbroken level per side
    can exist at a time and a quiet market may have neither within range.
    """
    events, swing_high, swing_low, trend_bias, closed = _replay_structure(df, length)
    if len(closed) < length * 2 + 5:
        return None
    forming = df.iloc[-1]

    candidates = []
    if swing_high is not None:
        level = swing_high["level"]
        dist_pct = (level - forming["close"]) / level * 100
        status = "testing" if forming["close"] > level else ("near" if abs(dist_pct) <= approach_threshold_pct else None)
        if status:
            would_be = "CHoCH" if trend_bias == -1 else "BOS"
            bars_held = (len(closed) - 1) - swing_high["pivot_bar"]
            candidates.append(ApproachingSMCSignal(
                symbol="", direction="long", status=status, would_be=would_be, level=round(level, 6),
                distance_pct=round(dist_pct, 2), bars_held=bars_held, close=float(forming["close"]),
                reason=(
                    f"{'Already closed above' if status == 'testing' else 'Approaching'} a swing high "
                    f"(${level:.6g}, held {bars_held} bars) -- would confirm {would_be} if it holds."
                ),
            ))
    if swing_low is not None:
        level = swing_low["level"]
        dist_pct = (forming["close"] - level) / level * 100
        status = "testing" if forming["close"] < level else ("near" if abs(dist_pct) <= approach_threshold_pct else None)
        if status:
            would_be = "CHoCH" if trend_bias == 1 else "BOS"
            bars_held = (len(closed) - 1) - swing_low["pivot_bar"]
            candidates.append(ApproachingSMCSignal(
                symbol="", direction="short", status=status, would_be=would_be, level=round(level, 6),
                distance_pct=round(dist_pct, 2), bars_held=bars_held, close=float(forming["close"]),
                reason=(
                    f"{'Already closed below' if status == 'testing' else 'Approaching'} a swing low "
                    f"(${level:.6g}, held {bars_held} bars) -- would confirm {would_be} if it holds."
                ),
            ))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c.status != "testing", abs(c.distance_pct)))
    return candidates[0]


def evaluate_zone_retest(
    df: pd.DataFrame, length: int = 20, volume_lookback: int = 20,
) -> ZoneRetestSignal | None:
    """Did the latest CLOSED candle dip into a still-ACTIVE (never-swept)
    liquidity zone and close back out the far side, holding it? direction=
    "long" = bounced off a support (swing-low) zone; "short" = rejected
    off a resistance (swing-high) zone. Requires the wick to have actually
    entered the zone (not just approached it) and the close to fully clear
    the far edge -- a weak half-hearted dip that closes mid-zone doesn't
    count as a clean hold.
    """
    events, swing_high, swing_low, _, closed = _replay_structure(df, length)
    if len(closed) < volume_lookback + 1:
        return None
    latest = closed.iloc[-1]
    prior = closed.iloc[:-1]
    avg_volume = prior["volume"].iloc[-volume_lookback:].mean()
    volume_ratio = float(latest["volume"] / avg_volume) if avg_volume > 0 else 0.0
    atr = float((prior["high"] - prior["low"]).iloc[-14:].mean())

    candidates = []
    if swing_low is not None:
        zone_top, zone_bottom = swing_low["zone_top"], swing_low["zone_bottom"]
        dipped_in = latest["low"] <= zone_top
        held = latest["close"] > zone_top and latest["close"] > latest["open"]
        if dipped_in and held and latest["low"] >= zone_bottom:
            depth_pct = (zone_top - latest["low"]) / (zone_top - zone_bottom) * 100 if zone_top > zone_bottom else 0.0
            score = volume_ratio * 2.0 + min(swing_low["touches"], 5) * 0.3 + min(depth_pct, 100) / 50
            stop_loss = zone_bottom - 0.5 * atr
            target = float(latest["close"]) + (float(latest["close"]) - stop_loss) * 2
            candidates.append(ZoneRetestSignal(
                symbol="", direction="long", score=round(score, 3), close=float(latest["close"]),
                zone_top=round(zone_top, 6), zone_bottom=round(zone_bottom, 6),
                stop_loss=round(stop_loss, 6), target=round(target, 6), touches=swing_low["touches"],
                volume_ratio=round(volume_ratio, 2), entry_time=latest["timestamp"],
                reason=(
                    f"Dipped into a support zone (${zone_bottom:.6g}-${zone_top:.6g}, {swing_low['touches']} "
                    f"prior test(s)) and reclaimed above it on {volume_ratio:.1f}x avg volume -- held, not broken."
                ),
            ))
    if swing_high is not None:
        zone_top, zone_bottom = swing_high["zone_top"], swing_high["zone_bottom"]
        poked_in = latest["high"] >= zone_bottom
        rejected = latest["close"] < zone_bottom and latest["close"] < latest["open"]
        if poked_in and rejected and latest["high"] <= zone_top:
            depth_pct = (latest["high"] - zone_bottom) / (zone_top - zone_bottom) * 100 if zone_top > zone_bottom else 0.0
            score = volume_ratio * 2.0 + min(swing_high["touches"], 5) * 0.3 + min(depth_pct, 100) / 50
            stop_loss = zone_top + 0.5 * atr
            target = float(latest["close"]) - (stop_loss - float(latest["close"])) * 2
            candidates.append(ZoneRetestSignal(
                symbol="", direction="short", score=round(score, 3), close=float(latest["close"]),
                zone_top=round(zone_top, 6), zone_bottom=round(zone_bottom, 6),
                stop_loss=round(stop_loss, 6), target=round(target, 6), touches=swing_high["touches"],
                volume_ratio=round(volume_ratio, 2), entry_time=latest["timestamp"],
                reason=(
                    f"Poked into a resistance zone (${zone_bottom:.6g}-${zone_top:.6g}, {swing_high['touches']} "
                    f"prior test(s)) and rejected back below it on {volume_ratio:.1f}x avg volume -- held, not broken."
                ),
            ))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[0]
