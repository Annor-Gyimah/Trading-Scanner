"""Live market data for REAL stocks -- actual NASDAQ/NYSE price action, real
session hours -- via yfinance. Distinct from Binance's tokenized TRADIFI
perpetuals (binance_data.get_tradfi_perp_universe), which track the same
underlying price but trade as a 24/7 synthetic derivative with no market
hours, no circuit breakers, and no real share custody. Nothing here places
an order, holds a brokerage account, or claims to be anything but Yahoo
Finance's public market data.
"""

from __future__ import annotations

import pandas as pd
import yfinance as yf

from .binance_data import get_tradfi_perp_universe

# yfinance has no native 4h bar, and a real 6.5-hour trading session doesn't
# divide cleanly into 4h buckets the way 24/7 crypto does (unlike the 15m/4h
# fallback used elsewhere). Rather than fake a 4h bar via a lopsided resample,
# real stocks only support these three -- ask for 1d if you want the
# accumulation tier's multi-day view.
SUPPORTED_TIMEFRAMES = ("15m", "1h", "1d")
_PERIOD_MAP = {"15m": "5d", "1h": "1mo", "1d": "2y"}


def get_real_stock_universe(limit: int | None = None) -> list[str]:
    """Plain tickers (e.g. 'AAPL', not a Binance pair) -- reuses Binance's
    tokenized-stock universe purely as a curated list of liquid, well-known
    real tickers. yfinance has no "list all tickers by volume" endpoint to
    discover this independently, so this borrows the list, not the data.
    """
    perp_symbols = get_tradfi_perp_universe(min_quote_volume_usd=0, limit=None)
    tickers = [s.split("/")[0] for s in perp_symbols]
    tickers = [t for t in tickers if t.isalpha() and 1 <= len(t) <= 5]
    return tickers[:limit] if limit else tickers


def fetch_stock_ohlcv_df(ticker: str, timeframe: str = "1h", limit: int = 120) -> pd.DataFrame:
    """Fetch real OHLCV candles for one US-listed ticker. Real market hours --
    expect gaps overnight and on weekends, not continuous bars like crypto."""
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError(
            f"Real stock data supports {SUPPORTED_TIMEFRAMES} only (yfinance has "
            f"no native 4h bar, and a 6.5h trading session doesn't divide cleanly "
            f"into one) -- got {timeframe!r}. Try --timeframe 1d."
        )
    raw = yf.Ticker(ticker).history(period=_PERIOD_MAP[timeframe], interval=timeframe)
    if raw.empty:
        raise ValueError(f"No data returned for {ticker}")
    raw = raw.tail(limit)
    df = raw.reset_index()
    time_col = "Datetime" if "Datetime" in df.columns else "Date"
    df = df.rename(columns={
        time_col: "timestamp", "Open": "open", "High": "high",
        "Low": "low", "Close": "close", "Volume": "volume",
    })
    return df[["timestamp", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
