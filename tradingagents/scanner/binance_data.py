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
