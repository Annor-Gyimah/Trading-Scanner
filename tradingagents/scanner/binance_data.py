"""Live market data access for the breakout scanner, via Binance's public USDT-M futures API.

Uses ccxt's public endpoints only (tickers/OHLCV) -- no API key needed, and
nothing in this module ever places an order.
"""

from __future__ import annotations

import re

import ccxt
import pandas as pd

_exchange = None


def timeframe_to_seconds(timeframe: str) -> int:
    match = re.fullmatch(r"(\d+)([mhd])", timeframe)
    if not match:
        raise ValueError(f"Can't parse timeframe {timeframe!r} (expected e.g. 15m, 1h, 4h, 1d)")
    value, unit = int(match.group(1)), match.group(2)
    return value * {"m": 60, "h": 3600, "d": 86400}[unit]


def get_exchange() -> ccxt.Exchange:
    global _exchange
    if _exchange is None:
        _exchange = ccxt.binanceusdm({"enableRateLimit": True})
    return _exchange


def _get_universe_by_contract_type(
    min_quote_volume_usd: float,
    limit: int | None,
    want_tradfi: bool,
) -> list[str]:
    ex = get_exchange()
    markets = ex.load_markets()
    tickers = ex.fetch_tickers()

    rows = []
    for symbol, market in markets.items():
        if not (market.get("swap") and market.get("quote") == "USDT" and market.get("active")):
            continue
        is_tradfi = market.get("info", {}).get("contractType") == "TRADIFI_PERPETUAL"
        if is_tradfi != want_tradfi:
            continue
        ticker = tickers.get(symbol)
        if not ticker:
            continue
        quote_volume = ticker.get("quoteVolume") or 0
        if quote_volume < min_quote_volume_usd:
            continue
        rows.append((symbol, quote_volume))

    rows.sort(key=lambda r: r[1], reverse=True)
    symbols = [s for s, _ in rows]
    return symbols[:limit] if limit else symbols


def get_liquid_perp_universe(
    min_quote_volume_usd: float = 2_000_000,
    limit: int | None = None,
) -> list[str]:
    """Return active crypto USDT-M perpetual symbols sorted by 24h quote volume, above a liquidity floor.

    The floor matters: thin futures markets -- the kind shown in pump-group
    "signal" screenshots -- are exactly where a handful of large orders can
    move price. Excluding them by default keeps the scanner out of the
    easiest-to-manipulate corner of the market.

    Also excludes Binance's tokenized TradFi perpetuals (stocks/ETFs/
    commodities -- SPY, QQQ, NVDA, XAU, XAG, etc., identifiable via
    info.contractType == "TRADIFI_PERPETUAL"; see get_tradfi_perp_universe()
    for those). These aren't crypto and don't share crypto's volatility/
    volume dynamics (no 10x-volume breakout candles the way an alt can
    print), so they were dominating tiers tuned for actual crypto behavior --
    e.g. SPY/QQQ/NVDA topping the accumulation watchlist on pure
    "tightest range" scoring, simply because mega-cap equities never move
    enough to disqualify themselves, not because they were a real setup.
    """
    return _get_universe_by_contract_type(min_quote_volume_usd, limit, want_tradfi=False)


def get_tradfi_perp_universe(
    min_quote_volume_usd: float = 2_000_000,
    limit: int | None = None,
) -> list[str]:
    """Return active tokenized-equity/ETF/commodity USDT-M perpetuals (AAPL, NVDA,
    TSLA, SPY, CL, ...), sorted by 24h quote volume, above a liquidity floor.

    These track the real underlying instrument's price but trade as a 24/7
    perpetual derivative on Binance, not on the instrument's actual exchange
    session -- no market hours, no circuit breakers, no settlement to the
    real security. Useful as real price-action data for the same detection
    logic used on crypto, but it is Binance's synthetic exposure, not a
    brokerage feed or the literal listed security.
    """
    return _get_universe_by_contract_type(min_quote_volume_usd, limit, want_tradfi=True)


def fetch_ohlcv_df(symbol: str, timeframe: str = "1h", limit: int = 120) -> pd.DataFrame:
    """Fetch recent OHLCV candles for one perpetual symbol as a DataFrame."""
    ex = get_exchange()
    raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


def fetch_ohlcv_history_df(symbol: str, timeframe: str = "1h", total_bars: int = 1000) -> pd.DataFrame:
    """Paginated OHLCV fetch for backtesting -- a single fetch_ohlcv call is
    capped at 1000 candles regardless of a higher `limit` (Binance's real
    per-request cap, not 1500 -- a single request for more than that always
    comes back short), so this walks `since` forward in chunks until
    `total_bars` candles are collected or the feed catches up to now,
    whichever comes first.

    Catching up to "now" has to be judged by comparing the last candle's
    own timestamp to the current time, NOT by whether a page came back
    shorter than requested -- every page is shorter than 1500 once
    `total_bars` exceeds 1000, which used to make this return after a
    single page no matter how far `since` still was from the present,
    silently truncating any total_bars > ~1000 to stale data."""
    ex = get_exchange()
    tf_ms = timeframe_to_seconds(timeframe) * 1000
    chunk = 1000
    since = ex.milliseconds() - total_bars * tf_ms
    rows: list[list] = []
    while len(rows) < total_bars:
        batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=chunk)
        if not batch:
            break
        rows.extend(batch)
        last_ts = batch[-1][0]
        if last_ts <= since:
            break  # exchange isn't advancing -- avoid spinning forever
        since = last_ts + tf_ms
        if since >= ex.milliseconds():
            break  # caught up to now
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df.tail(total_bars).reset_index(drop=True)
