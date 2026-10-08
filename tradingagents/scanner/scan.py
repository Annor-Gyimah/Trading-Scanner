"""Scan the liquid Binance USDT-M perpetual futures universe for early-stage breakouts.

Run directly: `python scan.py` from the repo root, or
`python -m tradingagents.scanner.scan`. See `python scan.py --help` for options.
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor

from . import colors
from .accumulation import AccumulationSignal, evaluate_accumulation
from .binance_data import (
    fetch_ohlcv_df,
    get_liquid_perp_universe,
    get_tradfi_perp_universe,
    timeframe_to_seconds,
)
from .breakout import (
    ApproachingSignal,
    BreakoutSignal,
    PullbackSignal,
    evaluate_approaching,
    evaluate_breakout,
    evaluate_pullback,
)
from .glossary import full_glossary_text, terms_for
from .gold_smc import GoldSMCSignal, evaluate_gold_smc
from .market_cap import base_ticker, fetch_market_cap_table
from .regime import RegimeSignal, evaluate_market_regime
from .stock_data import fetch_stock_ohlcv_df, get_real_stock_universe

GOLD_SYMBOL = "XAU/USDT:USDT"

# Parallel OHLCV fetches over ccxt's shared rate limiter. Tested at 200
# symbols: 10 workers finishes in ~12s with zero 429s, vs ~100s sequential --
# that gap matters because every second spent fetching is a second further
# from the candle close this result is supposed to reflect.
_FETCH_WORKERS = 10


def _get_universe(
    min_quote_volume_usd: float,
    universe_limit: int,
    max_market_cap_usd: float | None,
    market_caps: dict[str, dict],
    market: str = "crypto",
) -> tuple[list[str], dict[str, dict]]:
    """Shared symbol-universe + market-cap-filter logic used by every tier. Pure
    bookkeeping -- no detection logic lives here, so it can be shared without
    the tiers affecting each other's results.

    market="stocks" switches to Binance's tokenized equity/ETF/commodity
    perpetuals; market="stocks-real" switches to actual NASDAQ/NYSE price
    data via yfinance (real market hours, no 24/7 synthetic derivative --
    see stock_data.py). The CoinGecko market-cap filter has no effect on
    either stock mode (CoinGecko doesn't list stock tickers as coins, so
    every symbol falls into the "keep, absence isn't evidence" branch below)
    -- that's a real limitation, not a silent bug, and is called out in --help.
    """
    if market == "stocks-real":
        symbols = get_real_stock_universe(limit=universe_limit)
    else:
        get_universe = get_tradfi_perp_universe if market == "stocks" else get_liquid_perp_universe
        symbols = get_universe(min_quote_volume_usd=min_quote_volume_usd, limit=universe_limit)

    if max_market_cap_usd is not None:
        if not market_caps:
            print("Fetching market-cap data from CoinGecko...", file=sys.stderr)
            try:
                market_caps = fetch_market_cap_table()
            except Exception as exc:
                print(f"  market-cap lookup failed ({exc}); continuing without the cap filter", file=sys.stderr)
        if market_caps:
            before = len(symbols)
            symbols = [
                s for s in symbols
                if (info := market_caps.get(base_ticker(s))) is None or info["market_cap"] <= max_market_cap_usd
            ]
            print(
                f"  cap filter (<= ${max_market_cap_usd:,.0f}): {before} -> {len(symbols)} symbols "
                "(unlisted-on-CoinGecko symbols are kept, not excluded, since absence isn't evidence)",
                file=sys.stderr,
            )
    return symbols, market_caps


def _fetch_df(symbol: str, timeframe: str, market: str, limit: int = 120):
    """Dispatch to the right OHLCV source for `market` -- the only place that
    needs to know crypto/tokenized-stocks and real stocks come from different
    APIs. Every detection function downstream just gets a plain OHLCV frame."""
    if market == "stocks-real":
        return fetch_stock_ohlcv_df(symbol, timeframe=timeframe, limit=limit)
    return fetch_ohlcv_df(symbol, timeframe=timeframe, limit=limit)


def scan(
    timeframe: str = "1h",
    min_quote_volume_usd: float = 2_000_000,
    universe_limit: int = 200,
    top_n: int = 15,
    max_market_cap_usd: float | None = None,
    market: str = "crypto",
    _market_caps: dict[str, dict] | None = None,
) -> list[BreakoutSignal]:
    """Confirmed breakouts AND breakdowns -- both directions, clearly tagged via
    .direction. Nothing below this point is touched by the approaching tier."""
    symbols, market_caps = _get_universe(
        min_quote_volume_usd, universe_limit, max_market_cap_usd, _market_caps or {}, market
    )

    print(f"Scanning {len(symbols)} liquid {market} USDT-M perpetuals on {timeframe}...", file=sys.stderr)

    def _fetch(symbol: str):
        try:
            return symbol, _fetch_df(symbol, timeframe, market, limit=120), None
        except Exception as exc:
            return symbol, None, exc

    hits = []
    with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
        for symbol, df, exc in pool.map(_fetch, symbols):
            if exc is not None:
                print(f"  skip {symbol}: {exc}", file=sys.stderr)
                continue
            signal = evaluate_breakout(df)
            if signal is not None:
                signal.symbol = symbol
                info = market_caps.get(base_ticker(symbol))
                if info:
                    signal.market_cap = info["market_cap"]
                    signal.float_ratio = info["float_ratio"]
                hits.append(signal)

    longs = sorted((h for h in hits if h.direction == "long"), key=lambda s: s.score, reverse=True)
    shorts = sorted((h for h in hits if h.direction == "short"), key=lambda s: s.score, reverse=True)
    return longs[:top_n] + shorts[:top_n]


def scan_approaching(
    timeframe: str = "1h",
    min_quote_volume_usd: float = 2_000_000,
    universe_limit: int = 200,
    top_n: int = 15,
    max_market_cap_usd: float | None = None,
    market: str = "crypto",
    _market_caps: dict[str, dict] | None = None,
) -> list[ApproachingSignal]:
    """UNCONFIRMED watchlist tier, BOTH directions -- reads the live, still-forming candle.

    Entirely separate function from scan(): it is only ever called when
    --approaching is passed, and it cannot change what scan() returns.
    """
    symbols, _ = _get_universe(min_quote_volume_usd, universe_limit, max_market_cap_usd, _market_caps or {}, market)
    tf_seconds = timeframe_to_seconds(timeframe)

    def _fetch(symbol: str):
        try:
            return symbol, _fetch_df(symbol, timeframe, market, limit=120), None
        except Exception as exc:
            return symbol, None, exc

    hits = []
    with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
        for symbol, df, exc in pool.map(_fetch, symbols):
            if exc is not None:
                continue
            signal = evaluate_approaching(df, timeframe_seconds=tf_seconds)
            if signal is not None:
                signal.symbol = symbol
                hits.append(signal)

    # "Testing" (already through the level, unconfirmed) is more urgent than
    # "near" (still short of it); within each, closest first.
    def _sort_key(s):
        return (s.status != "testing", abs(s.distance_pct))

    longs = sorted((h for h in hits if h.direction == "long"), key=_sort_key)
    shorts = sorted((h for h in hits if h.direction == "short"), key=_sort_key)
    return longs[:top_n] + shorts[:top_n]


def scan_pullback(
    timeframe: str = "1h",
    min_quote_volume_usd: float = 2_000_000,
    universe_limit: int = 200,
    top_n: int = 15,
    max_market_cap_usd: float | None = None,
    market: str = "crypto",
    _market_caps: dict[str, dict] | None = None,
) -> list[PullbackSignal]:
    """Pullbacks following a recent strong impulse, in EITHER direction (long continuation
    or short reversal -- see evaluate_pullback's docstring for the difference).

    Entirely separate function from scan(): never called unless --pullback is
    passed, and it cannot change what scan() or scan_approaching() return.
    """
    symbols, _ = _get_universe(min_quote_volume_usd, universe_limit, max_market_cap_usd, _market_caps or {}, market)

    def _fetch(symbol: str):
        try:
            return symbol, _fetch_df(symbol, timeframe, market, limit=120), None
        except Exception as exc:
            return symbol, None, exc

    hits = []
    with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
        for symbol, df, exc in pool.map(_fetch, symbols):
            if exc is not None:
                continue
            signal = evaluate_pullback(df)
            if signal is not None:
                signal.symbol = symbol
                hits.append(signal)

    longs = sorted((h for h in hits if h.direction == "long"), key=lambda s: s.score, reverse=True)
    shorts = sorted((h for h in hits if h.direction == "short"), key=lambda s: s.score, reverse=True)
    return longs[:top_n] + shorts[:top_n]


def scan_accumulation(
    timeframe: str = "4h",
    min_quote_volume_usd: float = 2_000_000,
    universe_limit: int = 200,
    top_n: int = 15,
    max_market_cap_usd: float | None = None,
    market: str = "crypto",
    _market_caps: dict[str, dict] | None = None,
) -> list[AccumulationSignal]:
    """Symbols currently basing in a tight, not-yet-broken range -- the watchlist
    tier for catching a move before evaluate_breakout() ever fires on it.

    Entirely separate function from scan(): never called unless --accumulation
    is passed, and it cannot change what any other tier returns. Defaults to
    4h, not 1h, because a base worth watching plays out over days, not hours --
    see accumulation.py's calibration notes.
    """
    if market == "stocks-real" and timeframe == "4h":
        timeframe = "1d"  # yfinance has no 4h bar; 1d is the real equivalent "multi-day base" view
    symbols, _ = _get_universe(min_quote_volume_usd, universe_limit, max_market_cap_usd, _market_caps or {}, market)

    def _fetch(symbol: str):
        try:
            return symbol, _fetch_df(symbol, timeframe, market, limit=90), None
        except Exception as exc:
            return symbol, None, exc

    hits = []
    with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
        for symbol, df, exc in pool.map(_fetch, symbols):
            if exc is not None:
                continue
            signal = evaluate_accumulation(df)
            if signal is not None:
                signal.symbol = symbol
                hits.append(signal)

    hits.sort(key=lambda s: s.score, reverse=True)
    return hits[:top_n]


def scan_regime(timeframe: str = "1h", universe_limit: int = 60) -> RegimeSignal:
    """Crypto-wide bullish/neutral/bearish bias gauge -- see regime.py. Entirely
    separate code path: its own fetch of a breadth basket + BTC, independent of
    --universe-limit/--market caps used by every other tier."""
    return evaluate_market_regime(timeframe=timeframe, universe_limit=universe_limit)


def scan_gold_smc(htf_timeframe: str = "4h", ltf_timeframe: str = "15m") -> list[GoldSMCSignal]:
    """SMC liquidity-sweep analysis, scoped solely to XAU/USDT -- see gold_smc.py.

    Entirely separate code path: it is only ever called via --gold-smc, fetches
    only the gold symbol (ignores --universe-limit etc.), and touches nothing
    else in this file.
    """
    htf_df = fetch_ohlcv_df(GOLD_SYMBOL, timeframe=htf_timeframe, limit=100)
    ltf_df = fetch_ohlcv_df(GOLD_SYMBOL, timeframe=ltf_timeframe, limit=120)
    signal = evaluate_gold_smc(htf_df, ltf_df)
    return [signal] if signal else []


def _print_gold_smc(results: list[GoldSMCSignal]) -> None:
    if not results:
        print("No qualifying SMC sweep setup on XAU/USDT this scan.")
        return
    for s in results:
        label = colors.long_label("LONG") if s.direction == "long" else colors.short_label("SHORT")
        align = "aligned with" if s.aligned_with_bias else colors.dim("COUNTER to")
        print(f"\nXAU/USDT {label}   (HTF bias: {s.htf_bias}, {align} bias, in {s.premium_discount})")
        print(f"  Zone: {s.zone_kind} at ${s.zone_level:.2f}   Volume {s.volume_ratio:.1f}x avg")
        targets_str = "   ".join(colors.target(f"T{i + 1} {t:.2f}") for i, t in enumerate(s.targets))
        print(f"  {colors.entry(f'Entry {s.entry:.2f}')}   {colors.stop(f'Stop {s.stop_loss:.2f}')}   {targets_str}")
        print(f"  {s.reason}")
    print(
        "\nSMC sweep signal on gold only. Entry is the sweep candle's close -- that candle has "
        "already closed by the time this prints, so consider a limit order back near the zone "
        "level rather than market-chasing. A signal COUNTER to the HTF bias is a real, "
        "lower-conviction bounce setup, not a call that the trend has reversed.\n\n"
        + terms_for("gold_smc")
    )


def _print_regime(s: RegimeSignal) -> None:
    label = colors.long_label("BULLISH") if s.label == "bullish" else (
        colors.short_label("BEARISH") if s.label == "bearish" else colors.dim("NEUTRAL")
    )
    print(f"\n{colors.dim('MARKET REGIME')} ({s.score:+.0f}, {s.trend}): {label}")
    print(f"  {s.reason}")
    print(
        "  Context only, not a signal -- leans the kind of setup worth more attention "
        "(longs in a bullish regime, shorts in a bearish one) without filtering anything "
        "out. A bearish regime doesn't mean skip CONFIRMED LONG hits; it means weight "
        "CONFIRMED SHORT hits with more confidence than you would in a neutral tape."
    )


def _print_results_side(results: list[BreakoutSignal], direction: str) -> None:
    side = [s for s in results if s.direction == direction]
    label = (
        colors.long_label("CONFIRMED LONG (breakout)")
        if direction == "long"
        else colors.short_label("CONFIRMED SHORT (breakdown)")
    )
    print(f"\n{label}")
    print(f"{'Symbol':<16}{'Score':>7}{'Vol x':>8}{'RSI':>6}  Reason")
    print("-" * 110)
    if not side:
        print("(none this scan)")
        return
    for s in side:
        print(f"{s.symbol:<16}{s.score:>7.2f}{s.volume_ratio:>8.1f}{s.rsi:>6.0f}  {s.reason}")
        tps = "  ".join(colors.target(f"TP{i + 1} {tp:.6g}") for i, tp in enumerate(s.take_profits))
        print(
            f"{'':16}{colors.entry(f'Entry (market) {s.close:.6g}')}   "
            f"{colors.entry(f'Entry (retest) {s.breakout_level:.6g}')}   "
            f"{colors.stop(f'Stop {s.stop_loss:.6g}')}"
        )
        print(f"{'':16}{tps}")
        if s.market_cap is not None:
            cap_str = f"${s.market_cap:,.0f}"
            float_str = f"{s.float_ratio * 100:.0f}% circulating" if s.float_ratio is not None else "float n/a"
            print(f"{'':16}Market cap {cap_str}   ({float_str})")
        print()


def _print_results(results: list[BreakoutSignal]) -> None:
    _print_results_side(results, "long")
    _print_results_side(results, "short")
    print(
        "This is a technical screener, not a prediction. Every hit is a candidate for "
        "further research (liquidity, funding rate, broader market/BTC trend, any real "
        "news) -- not an auto-trade signal. Position size and leverage are your call.\n"
        "Entry (market) = current close, i.e. chasing it now. Entry (retest) = the "
        "broken level itself, i.e. waiting for a bounce/pullback back to it -- often "
        "never comes on a strong move, but risk per unit is smaller if it does. "
        "TP1/TP2/TP3 are staged exits at 0.5x/1.0x/1.618x the measured move; taking "
        "partial profit at TP1 and trailing the rest is a common way to use staged "
        "targets, not a rule."
        + ("\n\n" + terms_for("breakout") if results else "")
    )


def _print_approaching_side(results: list[ApproachingSignal], direction: str) -> None:
    side = [s for s in results if s.direction == direction]
    label = (
        colors.long_label("APPROACHING LONG (unconfirmed)")
        if direction == "long"
        else colors.short_label("APPROACHING SHORT (unconfirmed)")
    )
    print(f"\n{label}")
    print(f"{'Symbol':<16}{'Status':<9}{'Dist%':>7}{'Vol pace':>9}{'Bar%':>6}{'RSI':>6}  Reason")
    print("-" * 110)
    if not side:
        print("(none within threshold this scan)")
        return
    for s in side:
        print(
            f"{s.symbol:<16}{s.status:<9}{s.distance_pct:>7.1f}{s.volume_pace_ratio:>9.1f}"
            f"{s.bar_elapsed_pct:>6.0f}{s.rsi:>6.0f}  {s.reason}"
        )


def _print_approaching(results: list[ApproachingSignal]) -> None:
    _print_approaching_side(results, "long")
    _print_approaching_side(results, "short")
    print(
        "\nUNCONFIRMED -- these are mid-candle, not closed. They can and often do vanish "
        "before the bar closes (price pulls back, volume pace fades). Treat this purely as "
        "'what to have a chart open for,' not as something to enter on."
        + ("\n\n" + terms_for("approaching") if results else "")
    )


def _print_pullback_side(results: list[PullbackSignal], direction: str) -> None:
    side = [s for s in results if s.direction == direction]
    label = (
        colors.long_label("PULLBACK LONG (continuation)")
        if direction == "long"
        else colors.short_label("PULLBACK SHORT (reversal)")
    )
    print(f"\n{label}")
    print(f"{'Symbol':<16}{'Score':>7}{'Retrace%':>9}{'Vol x':>7}  Reason")
    print("-" * 110)
    if not side:
        print("(none this scan)")
        return
    for s in side:
        print(f"{s.symbol:<16}{s.score:>7.2f}{s.retrace_pct:>9.1f}{s.volume_ratio:>7.1f}  {s.reason}")
        tgts = "  ".join(colors.target(f"T{i + 1} {t:.6g}") for i, t in enumerate(s.targets))
        print(f"{'':16}{colors.entry(f'Entry {s.close:.6g}')}   {colors.stop(f'Stop {s.stop_loss:.6g}')}   {tgts}")
        print()


def _print_pullback(results: list[PullbackSignal]) -> None:
    _print_pullback_side(results, "long")
    _print_pullback_side(results, "short")
    print(
        "PULLBACK LONG is trend-aligned: buying a dip that's holding the level the original "
        "breakout broke above. PULLBACK SHORT is counter-trend: betting that level failed to "
        "hold at all -- a materially riskier bet that needs real confirmation (a clean close "
        "through the level on real volume), not just 'it came down a bit.' A normal pullback "
        "in a healthy trend dips below a level all the time without the trend being over; "
        "most of the time PULLBACK SHORT should stay empty even while PULLBACK LONG fires "
        "regularly, and that's the expected shape, not a bug."
        + ("\n\n" + terms_for("pullback") if results else "")
    )


def _print_accumulation(results: list[AccumulationSignal]) -> None:
    print(f"\n{colors.dim('ACCUMULATION (watchlist -- not broken out yet)')}")
    print(f"{'Symbol':<16}{'Score':>7}{'Range%':>8}{'ATR%':>6}{'Pos':>6}  Reason")
    print("-" * 110)
    if not results:
        print("(none this scan)")
        return
    for s in results:
        print(
            f"{s.symbol:<16}{s.score:>7.2f}{s.base_range_pct:>8.1f}{s.atr_pct:>6.1f}"
            f"{s.position_in_base:>6.2f}  {s.reason}"
        )
    print(
        "\nNOT a signal -- this is a watchlist of symbols coiling in a tight range, so you "
        "know what to watch for evaluate_breakout() (the CONFIRMED tier) to actually trigger "
        "on. 'Pos' is where price sits in the base now (0 = base low, 1 = base high); most "
        "bases never break out at all, and plenty break the wrong way -- basing alone isn't "
        "bullish, it's just quiet. Combine with --watch to get notified the moment one of "
        "these actually confirms, rather than checking back manually.\n\n"
        + terms_for("accumulation")
    )


def _beep() -> None:
    try:
        import winsound
        winsound.Beep(1000, 300)
    except Exception:
        print("\a", end="", flush=True)  # terminal bell fallback (non-Windows, or if winsound fails)


def watch(
    approaching: bool = False,
    pullback: bool = False,
    accumulation: bool = False,
    accumulation_timeframe: str = "4h",
    regime: bool = False,
    **scan_kwargs,
) -> None:
    """Re-run `scan` every time a new candle closes on `timeframe`, beeping on any hit.

    This is what actually closes the lag gap a one-off run can't: a signal
    found by manually re-running the scanner is only as fresh as how long ago
    you happened to run it. Aligning to candle close means you find out within
    seconds of a breakout confirming, not whenever you next check.

    With approaching=True, the unconfirmed watchlist is also refreshed every
    cycle -- same candle-close cadence as the confirmed scan, so it won't
    catch everything that happens mid-candle, only what's true right as each
    new candle opens. Fine as a first cut; ask if you want tighter polling.
    """
    timeframe = scan_kwargs.get("timeframe", "1h")
    tf_seconds = timeframe_to_seconds(timeframe)
    market_caps: dict[str, dict] = {}
    if scan_kwargs.get("max_market_cap_usd") is not None:
        print("Fetching market-cap data from CoinGecko (once, reused across the watch session)...", file=sys.stderr)
        try:
            market_caps = fetch_market_cap_table()
        except Exception as exc:
            print(f"  market-cap lookup failed ({exc}); continuing without the cap filter", file=sys.stderr)

    print(f"Watch mode: re-scanning every {timeframe} candle close. Ctrl+C to stop.\n", file=sys.stderr)
    try:
        while True:
            cycle_start = time.time()
            print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}]")
            if regime:
                _print_regime(scan_regime(timeframe=scan_kwargs.get("timeframe", "1h")))
            results = scan(**scan_kwargs, _market_caps=market_caps)
            print("\nCONFIRMED")
            _print_results(results)

            if approaching:
                approaching_results = scan_approaching(**scan_kwargs, _market_caps=market_caps)
                _print_approaching(approaching_results)

            if pullback:
                pullback_results = scan_pullback(**scan_kwargs, _market_caps=market_caps)
                _print_pullback(pullback_results)

            if accumulation:
                accumulation_kwargs = {**scan_kwargs, "timeframe": accumulation_timeframe}
                accumulation_results = scan_accumulation(**accumulation_kwargs, _market_caps=market_caps)
                _print_accumulation(accumulation_results)

            sys.stdout.flush()
            if results:
                _beep()

            now = time.time()
            next_close = (now // tf_seconds + 1) * tf_seconds
            sleep_for = max(5.0, next_close - now + 3)  # +3s buffer for the exchange to finalize the candle
            print(
                f"\nNext {timeframe} candle closes in {sleep_for:.0f}s "
                f"(this cycle took {now - cycle_start:.1f}s)...",
                file=sys.stderr,
            )
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        print("\nWatch mode stopped.", file=sys.stderr)


def watch_gold_smc(htf_timeframe: str = "4h", ltf_timeframe: str = "15m") -> None:
    """Re-check the XAU/USDT SMC setup every time the LTF (entry-trigger) candle closes.

    Separate loop from watch(): single symbol, no market-cap/approaching/
    pullback tiers, aligned to ltf_timeframe's candle close since that's the
    timeframe the actual sweep trigger lives on.
    """
    tf_seconds = timeframe_to_seconds(ltf_timeframe)
    print(
        f"Gold SMC watch mode: re-checking {GOLD_SYMBOL} every {ltf_timeframe} candle close "
        f"(HTF structure from {htf_timeframe}). Ctrl+C to stop.\n",
        file=sys.stderr,
    )
    try:
        while True:
            cycle_start = time.time()
            results = scan_gold_smc(htf_timeframe=htf_timeframe, ltf_timeframe=ltf_timeframe)
            print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}]")
            _print_gold_smc(results)
            sys.stdout.flush()
            if results:
                _beep()

            now = time.time()
            next_close = (now // tf_seconds + 1) * tf_seconds
            sleep_for = max(5.0, next_close - now + 3)
            print(
                f"\nNext {ltf_timeframe} candle closes in {sleep_for:.0f}s "
                f"(this cycle took {now - cycle_start:.1f}s)...",
                file=sys.stderr,
            )
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        print("\nGold SMC watch mode stopped.", file=sys.stderr)


def _parse_args():
    import argparse

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scan.py                                   1h scan, default settings
  python scan.py --timeframe 4h --top-n 5           4h scan, top 5 only
  python scan.py --timeframe 1d --max-market-cap 500000000
                                                     1d scan, only coins <= $500M cap
  python scan.py --watch --timeframe 15m            keep re-scanning every 15m candle close, beep on hits
  python scan.py --watch --timeframe 1h --max-market-cap 1000000000
                                                     watch mode + cap filter combined
  python scan.py --approaching                      ALSO show unconfirmed, still-forming setups
  python scan.py --watch --approaching --timeframe 15m
                                                     watch mode with both confirmed + approaching tiers
  python scan.py --pullback                         ALSO show post-breakout pullback setups (long + short)
  python scan.py --watch --pullback --approaching --timeframe 1h
                                                     watch mode with all three tiers combined
  python scan.py --gold-smc                         SMC liquidity-sweep analysis, XAU/USDT only
  python scan.py --gold-smc --watch                 keep re-checking gold every 15m candle close
  python scan.py --gold-smc --htf 1d --ltf 1h        override the HTF/LTF pair (defaults: 4h/15m)
  python scan.py --accumulation                     ALSO show symbols basing, not yet broken out
  python scan.py --watch --accumulation --pullback --approaching
                                                     watch mode with every tier combined
  python scan.py --glossary                        print term definitions (RSI, ATR, score, ...) and exit
  python scan.py --market stocks --timeframe 1d     scan tokenized stocks/ETFs/commodities instead of crypto
  python scan.py --market stocks --accumulation --timeframe 1d --accumulation-timeframe 1d
                                                     stock bases, daily candles
""",
    )
    parser.add_argument(
        "--timeframe",
        default="1h",
        help=(
            "Candle size to scan: 15m, 1h, 4h, 1d, etc. (default: 1h). Higher "
            "timeframes give fewer, slower, more reliable setups; lower "
            "timeframes give more, faster, noisier ones."
        ),
    )
    parser.add_argument(
        "--universe-limit", type=int, default=200,
        help="How many of the most liquid perpetuals to scan (default: 200).",
    )
    parser.add_argument(
        "--min-quote-volume", type=float, default=2_000_000,
        help="Liquidity floor in USD 24h quote volume (default: 2,000,000).",
    )
    parser.add_argument(
        "--top-n", type=int, default=15,
        help="How many ranked results to show (default: 15).",
    )
    parser.add_argument(
        "--max-market-cap", type=float, default=None,
        help=(
            "Only scan symbols at or below this USD market cap (via CoinGecko). "
            "Biases toward smaller, faster-moving coins -- but every hit still "
            "has to pass the same breakout confirmation as everything else. "
            "Low market cap on its own is not a signal: it means less capital "
            "is needed to move price a given %%, which cuts both ways and is "
            "the same trait that makes a coin easy to pump. Example: 500000000 "
            "for a $500M cap."
        ),
    )
    parser.add_argument(
        "--watch", action="store_true",
        help=(
            "Keep running: re-scan every time a new candle closes on --timeframe, "
            "and beep when something qualifies, instead of a single one-off scan. "
            "Ctrl+C to stop."
        ),
    )
    parser.add_argument(
        "--approaching", action="store_true",
        help=(
            "ALSO show an UNCONFIRMED watchlist tier, BOTH directions: symbols "
            "where the live, still-forming candle is already close to a "
            "breakout OR a breakdown level with volume pace building, read "
            "mid-candle instead of waiting for it to close. This is strictly "
            "additive -- off by default, and never changes what the confirmed "
            "scan (the table above it) reports. These can and do vanish "
            "before the candle closes; treat them as a watchlist, not a "
            "signal."
        ),
    )
    parser.add_argument(
        "--pullback", action="store_true",
        help=(
            "ALSO show pullbacks following a recent strong breakout, in BOTH "
            "directions, clearly separated: PULLBACK LONG (price pulled back "
            "and is holding the broken level -- trend-aligned continuation) "
            "and PULLBACK SHORT (price broke back below that level on real "
            "volume -- counter-trend, betting the breakout failed). Strictly "
            "additive -- off by default, never changes the confirmed or "
            "approaching tiers."
        ),
    )
    parser.add_argument(
        "--accumulation", action="store_true",
        help=(
            "ALSO show a watchlist of symbols currently basing in a tight, "
            "quiet range that HASN'T broken out yet -- so you know what to "
            "watch before the confirmed tier ever triggers on it. Strictly "
            "additive, off by default. NOT a bullish signal by itself: most "
            "bases never break out, and plenty break down instead of up -- "
            "it's a watchlist, not a prediction. Uses --accumulation-timeframe "
            "(default 4h), independent of --timeframe, since a base worth "
            "watching plays out over days, not within one hourly candle."
        ),
    )
    parser.add_argument(
        "--accumulation-timeframe", default="4h",
        help="Candle size for the accumulation/basing tier (default: 4h). Only used with --accumulation.",
    )
    parser.add_argument(
        "--regime", action="store_true",
        help=(
            "ALSO show a crypto-wide bullish/neutral/bearish bias gauge, blending "
            "breadth across a liquid basket with BTC's own trend, plus whether that "
            "bias is building, fading, or actively flipping. Context, not a filter -- "
            "it never removes a CONFIRMED hit, it just tells you which side of the "
            "book to weight more right now. Crypto-only; ignored with --market stocks*."
        ),
    )
    parser.add_argument(
        "--gold-smc", action="store_true",
        help=(
            "Run ONLY the SMC (Smart Money Concepts) liquidity-sweep analysis on "
            "the XAU/USDT gold perpetual, instead of the crypto-universe scan. "
            "Ignores --universe-limit/--min-quote-volume/--max-market-cap/"
            "--approaching/--pullback/--top-n -- none of those apply to a "
            "single-symbol, single-strategy mode. Combine with --watch to keep "
            "re-checking it on every LTF candle close. See --htf/--ltf."
        ),
    )
    parser.add_argument(
        "--htf", default="4h",
        help="Higher timeframe for gold SMC structure/bias/liquidity pools (default: 4h). Only used with --gold-smc.",
    )
    parser.add_argument(
        "--ltf", default="15m",
        help="Lower timeframe for the gold SMC sweep entry trigger (default: 15m). Only used with --gold-smc.",
    )
    parser.add_argument(
        "--glossary", action="store_true",
        help=(
            "Print a full plain-English glossary of every technical term this tool "
            "uses (RSI, ATR, score, retrace %%, premium/discount, etc.) and exit -- "
            "no scan runs. Useful on its own, or to hand to someone else using this "
            "tool so they can look up a term without re-reading the whole thing."
        ),
    )
    parser.add_argument(
        "--market", choices=["crypto", "stocks", "stocks-real"], default="crypto",
        help=(
            "Which universe to scan (default: crypto). 'stocks' scans Binance's "
            "tokenized equity/ETF/commodity PERPETUALS (AAPL, NVDA, TSLA, SPY, "
            "etc.) -- these track the real underlying price but trade 24/7 as a "
            "synthetic derivative, no market hours, no real share custody. "
            "'stocks-real' scans ACTUAL NASDAQ/NYSE price data via yfinance -- "
            "real session hours, real gaps overnight/weekends, nothing synthetic "
            "-- but only supports --timeframe 15m/1h/1d (yfinance has no native "
            "4h bar). --max-market-cap has no effect on either stock mode "
            "(CoinGecko doesn't list stock tickers). Only affects scan/"
            "approaching/pullback/accumulation; --gold-smc always targets "
            "XAU/USDT regardless."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if args.glossary:
        print(full_glossary_text())
        return

    if args.gold_smc:
        if args.watch:
            watch_gold_smc(htf_timeframe=args.htf, ltf_timeframe=args.ltf)
        else:
            results = scan_gold_smc(htf_timeframe=args.htf, ltf_timeframe=args.ltf)
            _print_gold_smc(results)
        return

    scan_kwargs = dict(
        timeframe=args.timeframe,
        min_quote_volume_usd=args.min_quote_volume,
        universe_limit=args.universe_limit,
        top_n=args.top_n,
        max_market_cap_usd=args.max_market_cap,
        market=args.market,
    )

    if args.watch:
        watch(
            approaching=args.approaching,
            pullback=args.pullback,
            accumulation=args.accumulation,
            accumulation_timeframe=args.accumulation_timeframe,
            regime=args.regime and args.market == "crypto",
            **scan_kwargs,
        )
        return

    if args.regime and args.market == "crypto":
        _print_regime(scan_regime(timeframe=args.timeframe))

    results = scan(**scan_kwargs)
    _print_results(results)

    if args.approaching:
        approaching_results = scan_approaching(**scan_kwargs)
        _print_approaching(approaching_results)

    if args.pullback:
        pullback_results = scan_pullback(**scan_kwargs)
        _print_pullback(pullback_results)

    if args.accumulation:
        accumulation_kwargs = {**scan_kwargs, "timeframe": args.accumulation_timeframe}
        accumulation_results = scan_accumulation(**accumulation_kwargs)
        _print_accumulation(accumulation_results)


if __name__ == "__main__":
    main()
