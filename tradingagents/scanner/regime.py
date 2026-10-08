"""Market-regime gauge: is the universe (and BTC, as the market's dominant proxy)
currently biased bullish or bearish, and is that bias building or fading?

This answers a different question than every other module in this package.
breakout.py/accumulation.py/gold_smc.py all ask "is THIS symbol doing something
right now." This asks "is the whole tape leaning one way right now, and should
that lean change which setups I even look at." It's context for every other
tier, not a signal of its own -- there's no entry/stop/target here.

Two ingredients, combined:
  - Breadth: across a basket of liquid symbols, what fraction are trending up
    (close > EMA20 > EMA50, rising) vs down (close < EMA20 < EMA50, falling)
    right now, on `timeframe`.
  - BTC trend: crypto alts correlate heavily with BTC, especially during
    risk-off moves (a BTC flash-crash drags the whole breadth score down with
    it, which is exactly what should happen -- it's not double-counting, it's
    BTC's outsized weight on the tape showing up twice because it really does
    matter twice as much as any one alt).

A single score isn't enough to answer "is it SWITCHING" -- a score of -40 could
mean "just started dropping" or "been bearish for days and stabilizing." So the
same score is also computed on the same data with the most recent `shift_bars`
bars dropped, and the delta between now and then is what actually answers the
user's question: is the bias building, fading, or flipping.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pandas as pd

from .binance_data import fetch_ohlcv_df, get_liquid_perp_universe

_FETCH_WORKERS = 10


@dataclass
class RegimeSignal:
    label: str  # "bullish" | "neutral" | "bearish"
    score: float  # -100..+100, breadth+BTC blended
    prior_score: float  # same score, as of `shift_bars` bars ago
    trend: str  # "strengthening" | "fading" | "flipping bullish" | "flipping bearish" | "steady"
    bullish_count: int
    bearish_count: int
    total: int
    btc_change_pct: float  # BTC's own % move over the lookback window
    reason: str


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _symbol_vote(df: pd.DataFrame, momentum_bars: int, trim_tail: int = 0) -> int | None:
    """+1 if this symbol is trending bullish, -1 bearish, 0 neutral/mixed, as of
    `trim_tail` candles ago (trim_tail=0 means "right now"). None if there isn't
    enough history to judge."""
    if trim_tail:
        df = df.iloc[: -trim_tail or None]
    closes = df["close"]
    if len(closes) < max(55, momentum_bars + 1):
        return None
    ema20 = _ema(closes, 20).iloc[-1]
    ema50 = _ema(closes, 50).iloc[-1]
    last_close = closes.iloc[-1]
    momentum_pct = (last_close / closes.iloc[-momentum_bars - 1] - 1) * 100

    if last_close > ema20 > ema50 and momentum_pct > 0:
        return 1
    if last_close < ema20 < ema50 and momentum_pct < 0:
        return -1
    return 0


def evaluate_market_regime(
    timeframe: str = "1h",
    universe_limit: int = 60,
    min_quote_volume_usd: float = 5_000_000,
    momentum_bars: int = 8,
    shift_bars: int = 6,
) -> RegimeSignal:
    """Blend basket breadth with BTC's own trend into one bullish/neutral/bearish
    read, plus whether that read is building, fading, or actively flipping.

    Crypto-only by design (both ingredients are crypto-specific: the liquid
    perp universe and BTC-as-market-proxy). Stocks have their own index
    proxies (SPY/QQQ) and this doesn't try to generalize to that yet.
    """
    symbols = get_liquid_perp_universe(min_quote_volume_usd=min_quote_volume_usd, limit=universe_limit)
    if "BTCUSDT" not in symbols:
        symbols = ["BTCUSDT"] + symbols

    fetch_limit = 50 + momentum_bars + shift_bars + 5

    def _fetch(symbol: str):
        try:
            return symbol, fetch_ohlcv_df(symbol, timeframe=timeframe, limit=fetch_limit), None
        except Exception as exc:
            return symbol, None, exc

    frames: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
        for symbol, df, exc in pool.map(_fetch, symbols):
            if exc is None and df is not None:
                frames[symbol] = df

    def _breadth_score(trim_tail: int) -> tuple[float, int, int, int]:
        bulls = bears = total = 0
        for df in frames.values():
            vote = _symbol_vote(df, momentum_bars, trim_tail)
            if vote is None:
                continue
            total += 1
            if vote > 0:
                bulls += 1
            elif vote < 0:
                bears += 1
        breadth = ((bulls - bears) / total * 100) if total else 0.0
        return breadth, bulls, bears, total

    breadth_now, bulls, bears, total = _breadth_score(trim_tail=0)
    breadth_prior, _, _, _ = _breadth_score(trim_tail=shift_bars)

    btc_df = frames.get("BTCUSDT")
    btc_change_now = 0.0
    btc_change_prior = 0.0
    if btc_df is not None and len(btc_df) > momentum_bars + shift_bars:
        closes = btc_df["close"]
        btc_change_now = (closes.iloc[-1] / closes.iloc[-momentum_bars - 1] - 1) * 100
        prior_closes = closes.iloc[: -shift_bars or None]
        btc_change_prior = (prior_closes.iloc[-1] / prior_closes.iloc[-momentum_bars - 1] - 1) * 100

    # BTC's % move, scaled so a +/-5% swing over the momentum window maps to +/-100,
    # blended evenly with breadth -- BTC is one symbol but it's the market's anchor,
    # so it gets as much weight here as the entire rest of the basket combined.
    btc_component_now = max(-100.0, min(100.0, btc_change_now / 5 * 100))
    btc_component_prior = max(-100.0, min(100.0, btc_change_prior / 5 * 100))

    score = 0.5 * breadth_now + 0.5 * btc_component_now
    prior_score = 0.5 * breadth_prior + 0.5 * btc_component_prior

    if score > 20:
        label = "bullish"
    elif score < -20:
        label = "bearish"
    else:
        label = "neutral"

    delta = score - prior_score
    prior_label = "bullish" if prior_score > 20 else "bearish" if prior_score < -20 else "neutral"
    if label != prior_label and label != "neutral":
        trend = f"flipping {label}"
    elif abs(delta) < 8:
        trend = "steady"
    elif delta > 0:
        trend = "fading" if label == "bearish" else "strengthening"
    else:
        trend = "fading" if label == "bullish" else "strengthening"

    reason = (
        f"breadth {bulls}/{total} bullish vs {bears}/{total} bearish "
        f"({breadth_now:+.0f} breadth score), BTC {btc_change_now:+.1f}% over last "
        f"{momentum_bars} {timeframe} candles -- {shift_bars} candles ago this same read was "
        f"{prior_score:+.0f} ({prior_label})"
    )

    return RegimeSignal(
        label=label,
        score=round(score, 1),
        prior_score=round(prior_score, 1),
        trend=trend,
        bullish_count=bulls,
        bearish_count=bears,
        total=total,
        btc_change_pct=round(btc_change_now, 2),
        reason=reason,
    )
