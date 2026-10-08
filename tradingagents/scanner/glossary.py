"""Plain-English definitions for every technical term the scanner prints.

Single source of truth so scan.py's per-tier footers and `--glossary` stay
consistent instead of each tier inventing its own wording. Every entry says
what the metric measures AND what a high/low reading means in practice --
the goal is that someone who has never seen these terms can read a result
line and understand it, not just recognize the column header.
"""

from __future__ import annotations

GLOSSARY: dict[str, str] = {
    "RSI": (
        "Relative Strength Index, 0-100: how fast and how far price has moved "
        "recently. LOW (under ~30-40) = oversold -- it's dropped hard and fast, "
        "which often (not always) precedes a bounce, but in a strong downtrend "
        "it can stay low and keep falling. HIGH (over ~70-80) = overbought -- "
        "it's risen hard and fast, often precedes a pause or pullback, but in a "
        "strong uptrend it can stay high and keep climbing. This scanner "
        "checks RSI as of the bar BEFORE any breakout/sweep, specifically to "
        "avoid entering something already pinned at an extreme."
    ),
    "ATR": (
        "Average True Range: the average size of a candle's full trading "
        "range (high to low) over a lookback, usually shown as a % of price "
        "so it's comparable across symbols. HIGH ATR% = a volatile symbol -- "
        "each candle swings a lot, so stops need to be wider and position size "
        "smaller to risk the same dollar amount. LOW ATR% = a calm symbol -- "
        "tighter stops are viable, but also smaller moves per candle."
    ),
    "VOLUME RATIO / VOL X": (
        "Current (or recent) volume divided by the average volume over a "
        "lookback. '3.0x' means 3 times the normal volume for that symbol -- "
        "real participation behind the move, not just price drifting on thin "
        "trading. Low volume ratio on a price move is a weaker, less trustworthy "
        "signal even if the price action looks similar."
    ),
    "SCORE": (
        "A ranking number from this tool's OWN formula (a mix of volume, "
        "tightness/compression, momentum, and similar factors specific to each "
        "tier). It exists purely to sort results best-first within THIS SCAN. "
        "It is NOT a probability, NOT a confidence level, and not comparable "
        "between different tiers (breakout score and accumulation score use "
        "completely different formulas) -- a 'score 14' breakout hit and a "
        "'score 14' accumulation hit have nothing numerically in common."
    ),
    "RETRACE %": (
        "How much of a prior move has been given back, as a % of that move's "
        "total range. Near 0% = barely pulled back yet. Near 100% = round-"
        "tripped almost the entire move back to where it started -- getting "
        "marginal as a 'healthy pullback' and starting to look like the move "
        "failed. Over 100% means price broke back past where the move began."
    ),
    "DIST % (distance to level)": (
        "How far current price sits from a specific level (a breakout level, "
        "a liquidity pool, etc.), as a %. Smaller = closer to that level right "
        "now. A NEGATIVE distance in the approaching tier means price is "
        "already on the other side of the level, unconfirmed."
    ),
    "VOLUME PACE": (
        "Used only for the still-forming candle (the approaching tier): "
        "current volume so far this candle, projected to what a FULL candle's "
        "volume would be at this rate, divided by the normal average. Lets you "
        "judge participation mid-candle instead of waiting for it to close. "
        "HIGH pace early in a candle is a stronger signal than the same pace "
        "right before the candle closes anyway."
    ),
    "BAR %": (
        "How far through the still-forming candle we are (0% = just opened, "
        "100% = about to close). Context for how much to trust the volume "
        "pace number above -- a volume pace reading at 10% through the bar is "
        "far noisier than one at 90% through it."
    ),
    "RANGE % (accumulation)": (
        "How wide a symbol's trading range has been over the base window, "
        "top to bottom, as a %. LOW range% = tightly coiled, barely moving -- "
        "the 'quiet' half of quiet-then-loud. HIGH range% means it's been "
        "choppier, less of a clean coil."
    ),
    "POS (position in base)": (
        "Where current price sits inside the accumulation range: 0 = sitting "
        "right at the base's low, 1 = sitting right at the base's high, 0.5 = "
        "dead center. Doesn't predict which way it breaks -- just tells you "
        "where in the range it currently is."
    ),
    "BIAS (SMC)": (
        "The higher-timeframe structural lean -- bullish (higher highs AND "
        "higher lows), bearish (lower highs AND lower lows), or ranging "
        "(neither, or mixed). This is a READ of recent structure, not a "
        "prediction of what happens next."
    ),
    "PREMIUM / DISCOUNT (SMC)": (
        "Where current price sits relative to the midpoint (equilibrium) of "
        "the active trading range. DISCOUNT = below the midpoint, the 'cheap' "
        "half. PREMIUM = above the midpoint, the 'expensive' half. In SMC "
        "terms this is where buyers/sellers are thought to be more willing to "
        "act -- it does NOT by itself mean price will reverse from there."
    ),
    "LIQUIDITY POOL / EQUAL HIGHS/LOWS (SMC)": (
        "A price level that's been touched more than once by recent swing "
        "highs or lows. These are where resting stop-losses and breakout "
        "orders are assumed to cluster -- a 'pool' of orders a move can "
        "sweep through before reversing."
    ),
    "LIQUIDITY SWEEP (SMC)": (
        "A wick through one of those pool levels that fails to hold and "
        "closes back on the other side -- read as the resting orders at that "
        "level being triggered and absorbed, rather than genuine continuation "
        "through it."
    ),
}

# Which glossary keys matter for each tier's printed columns -- lets each
# footer pull only the terms it actually used instead of the whole glossary.
TIER_TERMS: dict[str, list[str]] = {
    "breakout": ["SCORE", "VOLUME RATIO / VOL X", "RSI"],
    "approaching": ["DIST % (distance to level)", "VOLUME PACE", "BAR %", "RSI"],
    "pullback": ["SCORE", "RETRACE %", "VOLUME RATIO / VOL X"],
    "accumulation": ["SCORE", "RANGE % (accumulation)", "ATR", "POS (position in base)"],
    "gold_smc": [
        "BIAS (SMC)",
        "PREMIUM / DISCOUNT (SMC)",
        "LIQUIDITY POOL / EQUAL HIGHS/LOWS (SMC)",
        "LIQUIDITY SWEEP (SMC)",
        "VOLUME RATIO / VOL X",
    ],
}


def terms_for(tier: str) -> str:
    """One formatted block explaining every term used by `tier`'s columns."""
    keys = TIER_TERMS.get(tier, [])
    lines = [f"  {k}: {GLOSSARY[k]}" for k in keys]
    return "Terms used above:\n" + "\n".join(lines)


def full_glossary_text() -> str:
    lines = ["=" * 78, "SCANNER GLOSSARY -- every technical term this tool prints, explained", "=" * 78]
    for term, definition in GLOSSARY.items():
        lines.append(f"\n{term}")
        lines.append(f"  {definition}")
    lines.append("\n" + "=" * 78)
    lines.append(
        "General reminder: every number here describes the PAST and PRESENT -- "
        "what has already happened on the chart -- not a prediction of what "
        "happens next. Treat all of it as research input, not as instructions."
    )
    return "\n".join(lines)
