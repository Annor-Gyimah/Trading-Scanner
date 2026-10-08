"""Market-cap / float context from CoinGecko's public API (no key required).

This does NOT drive the breakout/sweep decision -- low float is a volatility
amplifier (less capital needed to move price a given %), not a directional
signal. It cuts both ways, and it's the same mechanism behind a manipulated
pump as behind a genuine small-cap breakout. Use it to bias *which symbols*
get scanned (smaller cap = more room to run per dollar of real volume) while
the actual entry trigger stays the technical confirmation in breakout.py --
never supply stats alone.
"""

from __future__ import annotations

import time

import requests

COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"

# CoinGecko's free tier throttles rapid sequential requests (429s observed at
# ~6 requests fired back to back). A few seconds between pages keeps a 6-page
# pull comfortably under that limit instead of failing partway through.
_PAGE_DELAY_SECONDS = 2.5


def fetch_market_cap_table(pages: int = 6, per_page: int = 250) -> dict[str, dict]:
    """Return {TICKER: {market_cap, circulating_supply, max_supply, float_ratio, ...}}.

    Keyed by uppercase ticker symbol. CoinGecko tickers can collide between
    unrelated coins (e.g. a major asset and an obscure one sharing a symbol);
    on collision this keeps whichever has the larger market cap, since that's
    almost always the asset a Binance USDT-M perpetual with that symbol
    actually tracks.
    """
    table: dict[str, dict] = {}
    for page in range(1, pages + 1):
        rows = None
        for attempt in range(3):
            resp = requests.get(
                COINGECKO_MARKETS_URL,
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": per_page,
                    "page": page,
                    "sparkline": "false",
                },
                timeout=20,
            )
            if resp.status_code == 429:
                time.sleep(_PAGE_DELAY_SECONDS * (attempt + 2))
                continue
            resp.raise_for_status()
            rows = resp.json()
            break
        if not rows:
            break
        if page < pages:
            time.sleep(_PAGE_DELAY_SECONDS)
        for row in rows:
            ticker = (row.get("symbol") or "").upper()
            if not ticker:
                continue
            market_cap = row.get("market_cap") or 0
            existing = table.get(ticker)
            if existing and existing["market_cap"] >= market_cap:
                continue
            circulating = row.get("circulating_supply")
            max_supply = row.get("max_supply") or row.get("total_supply")
            float_ratio = (
                circulating / max_supply if circulating and max_supply and max_supply > 0 else None
            )
            table[ticker] = {
                "market_cap": market_cap,
                "circulating_supply": circulating,
                "max_supply": max_supply,
                "float_ratio": float_ratio,
            }
    return table


def base_ticker(perp_symbol: str) -> str:
    """'ATH/USDT:USDT' -> 'ATH'."""
    return perp_symbol.split("/")[0].upper()
