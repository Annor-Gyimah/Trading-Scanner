"""A live dashboard for the scanner, built on Textual.

Pure presentation layer: every number on screen comes from the exact same
scan()/scan_approaching()/scan_pullback()/scan_accumulation()/scan_smc()
functions the CLI (scan.py) uses. Nothing here recomputes a signal or
duplicates detection logic -- this only decides how to lay it out, color it,
and keep it refreshed.
"""

from __future__ import annotations

import calendar as calendar_mod
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from rich.table import Table
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    Select,
    Static,
    Switch,
    TabbedContent,
    TabPane,
)
from tradingagents.scanner import (
    credentials_configured,
    evaluate_market_regime,
    execute_propr_signals,
    execute_signals,
    fetch_crypto_news,
    fetch_stock_news,
    get_daily_pnl,
    get_propr_orders,
    get_propr_stats,
    get_stats,
    get_testnet_orders,
    get_testnet_stats,
    resolve_propr_orders,
    resolve_testnet_orders,
    get_trades,
    propr_credentials_configured,
    record_signal,
    resolve_open_trades,
    run_backtest,
    scan,
    scan_accumulation,
    scan_approaching,
    scan_pullback,
    scan_smc,
    scan_smc_approaching,
    scan_zone_retest,
    timeframe_to_seconds,
)

from .favorites_store import load_favorites, save_favorites
from .settings_store import load_settings, save_settings

CSS_FILE = Path(__file__).parent / "theme.tcss"

TIMEFRAMES = [("15m", "15m"), ("1h", "1h"), ("4h", "4h"), ("1d", "1d")]
MARKETS = [("Crypto", "crypto"), ("Stocks (perp)", "stocks"), ("Real Stocks", "stocks-real")]

# Tabs that actually join the auto-refresh rotation (Backtest is manual-only,
# Favorites/Testnet are passive logs, nothing to "refresh" on a timer) --
# configurable per-tab in Settings so a slow one can be scanned on demand
# (the Scan Now button) instead of eating into every auto-refresh cycle.
AUTO_REFRESH_TABS = [
    ("tab-confirmed", "Confirmed"),
    ("tab-approaching", "Approaching"),
    ("tab-pullback", "Pullback"),
    ("tab-accumulation", "Accumulation"),
    ("tab-structure", "Structure"),
    ("tab-news", "News"),
    ("tab-paper", "Paper"),
    ("tab-testnet", "Testnet"),
    ("tab-propr", "Propr"),
]

# yfinance (Real Stocks) has no native 4h bar -- see stock_data.py. Coerced
# to 1d automatically rather than erroring on every single symbol.
_NO_4H_MARKETS = {"stocks-real"}

_LONG_STYLE = "bold #3fb950"
_SHORT_STYLE = "bold #f85149"
_ENTRY_STYLE = "bold #58a6ff"
_STOP_STYLE = "bold #f85149"
_TARGET_STYLE = "bold #3fb950"
_DIM_STYLE = "#6e7681"

# Two distinct auto-refresh events, two distinct tones: a full candle-close
# scan (pitch depends on which timeframe is selected, so 15m/1h/4h/1d are
# told apart by ear) vs. a 15/30/45-min fast-refresh checkpoint (one fixed
# tone, regardless of which fast-refresh tab fired -- see
# _FAST_REFRESH_TABS). Higher timeframe -> lower pitch, on the intuition
# that a slower/bigger-picture event should sound heavier, not more urgent.
_TIMEFRAME_TONES_HZ = {"15m": 900, "1h": 700, "4h": 500, "1d": 350}
_CHECKPOINT_TONE_HZ = 600
_TONE_DURATION_MS = 150


def _play_tone(freq_hz: int, duration_ms: int = _TONE_DURATION_MS) -> None:
    """Fire-and-forget a short beep off the main thread -- winsound.Beep
    blocks for the full duration, which would freeze the UI if called
    directly from the event loop. Falls back to the terminal bell outside
    Windows, same fallback the CLI's _beep() uses."""
    def _play() -> None:
        try:
            import winsound
            winsound.Beep(freq_hz, duration_ms)
        except Exception:
            print("\a", end="", flush=True)
    threading.Thread(target=_play, daemon=True).start()


def _rsi_text(value: float) -> Text:
    """Color RSI by what it means: cold (oversold), hot (overbought), neutral."""
    if value <= 35:
        return Text(f"{value:.0f}", style="bold #58a6ff")  # cold
    if value >= 70:
        return Text(f"{value:.0f}", style="bold #f0b429")  # hot
    return Text(f"{value:.0f}", style="#c9d1d9")


def _vol_text(ratio: float) -> Text:
    style = "bold #3fb950" if ratio >= 2 else "#c9d1d9"
    return Text(f"{ratio:.1f}x", style=style)


_FAV_STAR_STYLE = "bold #f0b429"


class GlossaryScreen(ModalScreen):
    """Full-screen reference card -- every term the dashboard uses, explained."""

    BINDINGS = [("escape", "dismiss", "Close"), ("g", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="glossary-panel"):
            yield Label("GLOSSARY", id="glossary-title")
            with VerticalScroll(id="glossary-body"):
                yield Markdown(_glossary_as_markdown())
            yield Label("Press Esc or g to close", id="glossary-close-hint")

    def action_dismiss(self, result=None) -> None:
        self.app.pop_screen()


class HowToScreen(ModalScreen):
    """Full-screen trading guide -- how to read each tier and what to weigh
    before actually entering a trade. Lives alongside Glossary/Settings
    because it answers "what do I do with this" rather than "what does this
    term mean."""

    BINDINGS = [("escape", "dismiss", "Close"), ("h", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="howto-panel"):
            yield Label("HOW TO CHOOSE A SYMBOL", id="howto-title")
            with VerticalScroll(id="howto-body"):
                yield Markdown(_HOWTO_TEXT)
            yield Label("Press Esc or h to close", id="howto-close-hint")

    def action_dismiss(self, result=None) -> None:
        self.app.pop_screen()


_HOWTO_TEXT = """\
Each tier answers a different question. Reading the wrong column for the \
wrong tier is the most common mistake -- this page walks through what \
actually matters at each stage, with real examples.

## Approaching -- "what to have a chart pulled up on"

This reads the still-forming candle, so it can vanish before the bar \
closes. It is a watchlist, not a signal. Two columns actually predict \
whether a row survives into Confirmed:

- **Status**: `testing` (already past the level, unconfirmed) is the \
  group that matters -- `near` hasn't broken the level yet, so it has \
  further to go.
- **Vol pace**: a *projected* full-bar ratio. Confirmed needs the \
  *realized* volume at close to clear 2x average, so a `testing` row at \
  10.9x pace has far more room to fall off and still clear that bar than \
  one sitting at 1.8x.
- **RSI** is a ceiling check, not a ranking signal. Confirmed-long caps \
  at RSI 82 (measured on the candle *before* the breakout). A `testing` \
  row already at RSI 78 is one tick from being disqualified outright, not \
  just lower-ranked.
- **Bar%**: how much of the candle is still unwritten. At 46% elapsed, \
  over half the move can still happen (or unwind) before this becomes \
  real. Nothing in this tab is tradeable yet.

**Example**: ZK/USDT showed up `testing`, -2.2% (already past the level), \
10.9x volume pace, RSI 67, 46% through the bar. High realized-volume \
headroom and RSI nowhere near the 82 ceiling -- exactly the profile that \
survives into Confirmed on the next scan, which is what happened.

## Confirmed -- the actual trade candidates

Built from the fully closed candle only, so it can't repaint. The score \
is `volume_ratio x2.0 + compression_bonus x3.0 + close_position x2.0 + \
(RSI headroom) x0.05`. In practice, weigh it in this order:

1. **Volume ratio first.** This is the real-participation gate. A 21.6x \
   reading is a structurally different, more real move than a 2.1x \
   squeaker, even if they land on a similar score.
2. **RSI second, as a risk filter, not a ranking tool.** How close is it \
   to the band edge it isn't allowed to cross (82 for longs, 18 for \
   shorts)? Close to the edge means less room before the move is \
   mechanically "overbought/oversold" -- exactly the setups most prone to \
   a sharp pullback before continuing, if they continue at all.
3. **Score last**, as a tiebreaker once volume and RSI both check out. It \
   blends everything, so two symbols can land on the same score for \
   different reasons (one from compression, one from raw volume) -- the \
   score alone won't tell you which.
4. **Regime, above all of the above.** A Confirmed short in a BEARISH \
   regime has the trend behind it. A Confirmed long in that same regime \
   is a counter-trend bet fighting the tape -- counter-trend breakouts are \
   the ones most likely to fire, dip, and stop you out before (maybe) \
   continuing. When the regime banner and the signal direction disagree, \
   that disagreement matters more than score or RSI.

**Example**: a Confirmed long firing while the regime banner reads \
BEARISH (-20, flipping bearish) is the riskier side of that trade from \
the start, independent of how high its score is -- size down or skip, \
rather than trusting the score to have already priced that in.

## Pullback -- confirmation of a prior move, not a fresh signal

Only fires on symbols that already had a recent impulse (a breakout in \
the last `impulse_window` bars), and only after at least one full candle \
has passed since that impulse closed -- by design, so it never reacts to \
the very candle that just broke out.

- **direction=long** requires the broken level to still be holding \
  (close above it) and some sign the dip is arresting (green close, or a \
  higher low than the bar before). The reason text literally says \
  "broken resistance now acting as support" -- that is the exact pattern \
  being flagged.
- **direction=short** is a materially higher bar: a real close back \
  *below* the level plus volume on that break >= 1.3x average. A normal \
  pullback dipping below a level briefly does not qualify -- it takes a \
  clean reclaim with real participation to call the breakout failed.

**Example**: S/USDT confirmed long, then showed up in Pullback as price \
retraced and held above the broken level, then confirmed long again as \
it resumed. That is not three disconnected flags -- it is the retest-\
and-continue pattern playing out across tiers exactly as designed. Seeing \
a symbol move Confirmed -> Pullback -> Confirmed again is a sign the \
tiers are tracking one coherent price story, not noise.

## Structure tab -- a second, independent lens (BOS/CHoCH + liquidity zones)

Everything above reads price in terms of N-bar highs/lows. The Structure \
tab reads the same candles a completely different way -- swing-point \
market structure, adapted from LuxAlgo's Smart Money Concepts and \
Liquidity Swings indicators -- and can agree or disagree with Confirmed/ \
Approaching/Pullback on the very same symbol. Three sections, each a \
different question:

- **CONFIRMED STRUCTURE BREAK** -- a swing high/low just got closed \
  through. **BOS** (break of structure) means the break continues the \
  prevailing trend; **CHoCH** (change of character) means it just \
  *flipped* the trend -- the first break against whatever direction was \
  previously in control. "Held" is how many bars that swing point stood \
  before breaking (a longer-held level has more participants anchored to \
  it, in theory); "Touches" is how many times price poked the zone \
  without breaking it first; "Pool" flags when the broken level was part \
  of an equal-high/low liquidity cluster rather than a lone swing.
- **APPROACHING STRUCTURE LEVEL** -- the live, still-forming candle is \
  testing or nearing a swing level that would confirm a BOS/CHoCH if it \
  holds through the close. Same "watchlist, can vanish" rule as the \
  regular Approaching tab.
- **ZONE RETEST** -- the opposite situation from the break tables: price \
  dipped *into* a zone that has never broken, and closed back out the \
  far side, holding it. "Bounced off support" (long) or "rejected at \
  resistance" (short). This is the pattern behind price returning to a \
  support/resistance box and reacting from it instead of breaking through \
  -- the zone is a box (wick to candle body), not a single price, so a \
  bounce that starts partway into the zone still counts, not just one \
  exact level.

**How to select a pair here, honestly**: do not treat BOS/CHoCH's score \
or Zone Retest's score as a ranking of quality yet -- a real historical \
backtest (267 BOS/CHoCH trades and 382 zone-retest trades across 50 \
liquid symbols) came back roughly flat overall (avg R close to zero for \
both), and splitting by score quartile showed the highest-scored trades \
did NOT outperform the lowest-scored ones. That is different from \
Confirmed's volume/RSI gates, which did hold up under the same kind of \
check. Treat Structure as exploratory context -- useful for seeing *why* \
a symbol moved the way the chart shows (a genuine trend flip vs. just \
another N-bar breakout, or a level holding vs. breaking), not yet as \
something to size a trade on by itself. The one semi-consistent thread \
across both backtests: a level/zone with a LOT of history behind it \
(very high touch count, or held for a very long time) did noticeably \
better than a middling one -- weak evidence, not proof, but worth \
weighting if you do act on a Structure row.

**Both directions, every scan.** Structure, Approaching-structure, and \
Zone Retest all scan long and short every cycle, same as every other \
tier -- nothing here is upside-only. It is also in the auto-refresh \
rotation (checked every 15/30/45 minutes within the candle, not just \
once at close), the same fast cadence as Approaching/Paper/Testnet, \
since the approaching-structure half reads the live candle too.

## Putting it together

There is no single column that replaces judgment. The fast version: \
Approaching tells you what to watch, Confirmed's volume ratio and RSI \
tell you how real and how stretched the move already is, the regime \
banner tells you whether you are trading with or against the tape, \
Pullback tells you whether a level is actually holding on a retest, and \
Structure gives you a second opinion built on swing points and liquidity \
zones instead of N-bar highs/lows -- interesting context, not yet a \
validated edge on its own. Score is a useful sort order within a tier \
that already has a proven gate (Confirmed, Pullback) -- for Structure, \
read the reason text and the touch/held counts yourself rather than \
trusting the number.
"""


class SettingsScreen(ModalScreen):
    """App-wide settings that affect future behavior, not current data --
    currently just paper-trading sizing. A home for anything that would
    otherwise clutter a tab's own control bar with cross-cutting config.

    Changes here take effect immediately for whatever happens NEXT (the next
    signal logged), but never retroactively touch trades already in the
    ledger -- each trade locks in whatever was set at the moment it was
    recorded (see paper_trading.record_signal's docstring)."""

    BINDINGS = [("escape", "dismiss", "Close"), ("s", "dismiss", "Close")]

    def __init__(self, leverage: float, notional: float, testnet_enabled: bool, propr_enabled: bool) -> None:
        super().__init__()
        self._leverage = leverage
        self._notional = notional
        self._testnet_enabled = testnet_enabled
        self._propr_enabled = propr_enabled
        self._creds_ok = credentials_configured()
        self._propr_creds_ok = propr_credentials_configured()

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-panel"):
            yield Label("SETTINGS", id="settings-title")
            with VerticalScroll(id="settings-body"):
                yield Label("Paper trading -- new trades only", classes="panel-title")
                yield Label(
                    "Changes apply to trades logged from now on. Every trade already in the "
                    "ledger keeps whatever margin/leverage was set when IT was logged, forever.",
                    classes="panel-subtitle",
                )
                with Horizontal(classes="settings-row"):
                    yield Label("Margin per trade")
                    yield Select(
                        [("$10", "10"), ("$25", "25"), ("$50", "50"), ("$100", "100"), ("$500", "500")],
                        value=str(int(self._notional)), id="settings-notional-select", allow_blank=False,
                    )
                with Horizontal(classes="settings-row"):
                    yield Label("Leverage")
                    yield Select(
                        [("1x (none)", "1"), ("2x", "2"), ("3x", "3"), ("5x", "5"), ("10x", "10"), ("20x", "20")],
                        value=str(int(self._leverage)), id="settings-leverage-select", allow_blank=False,
                    )
                yield Label(
                    "Leverage only scales the $ P&L figure -- it never changes R-multiple, and this "
                    "model doesn't simulate liquidation risk (see the glossary's LEVERAGE entry).",
                    classes="panel-subtitle",
                )
                yield Label("Testnet auto-execution", classes="panel-title")
                if self._creds_ok:
                    with Horizontal(classes="settings-row"):
                        yield Label("Place real orders on Binance Futures Testnet", id="settings-testnet-label")
                        yield Switch(value=self._testnet_enabled, id="settings-testnet-switch")
                    yield Label(
                        "Fake funds, a real matching engine -- every new CONFIRMED signal (crypto "
                        "only) gets a market entry plus reduce-only stop/TP1, sized by the margin/"
                        "leverage above. See the Testnet tab for the order log.",
                        classes="panel-subtitle",
                    )
                else:
                    yield Label(
                        "Not available -- set BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET "
                        "(create both free at https://testnet.binancefuture.com/) and restart the app.",
                        classes="panel-subtitle",
                    )
                yield Label("Propr dry-run execution", classes="panel-title")
                if self._propr_creds_ok:
                    with Horizontal(classes="settings-row"):
                        yield Label("Log what Propr orders WOULD be (never sent)", id="settings-propr-label")
                        yield Switch(value=self._propr_enabled, id="settings-propr-switch")
                    yield Label(
                        "DRY RUN ONLY -- there is no separate test account on Propr, so this never "
                        "calls the real orders endpoint. Every new CONFIRMED signal (crypto only) "
                        "that's actually tradable on Hyperliquid gets logged with the exact order it "
                        "would place, resolved against live mark price. See the Propr tab.",
                        classes="panel-subtitle",
                    )
                else:
                    yield Label(
                        "Not available -- set PROPR_API_KEY (from app.propr.xyz settings) and restart the app.",
                        classes="panel-subtitle",
                    )
                yield Label("Auto-refresh tabs", classes="panel-title")
                yield Label(
                    "Auto-refresh (the a key, or the switch up top) only refreshes the tabs checked "
                    "here. Uncheck a slow one and it stays manual -- use Scan Now or r to refresh it "
                    "yourself, without it eating into every cycle.",
                    classes="panel-subtitle",
                )
                for tab_id, label in AUTO_REFRESH_TABS:
                    with Horizontal(classes="settings-row"):
                        yield Label(label, id=f"autoref-label-{tab_id}")
                        yield Switch(value=tab_id in self.app._auto_refresh_tabs, id=f"autoref-{tab_id}")
            yield Label("Press Esc or s to close", id="settings-close-hint")

    def on_switch_changed(self, event: Switch.Changed) -> None:
        switch_id = event.switch.id or ""
        if switch_id == "settings-testnet-switch":
            self.app._testnet_enabled = event.value
            self.app._set_status(f"Testnet auto-execution {'ON' if event.value else 'OFF'}")
            self.app._save_settings()
        elif switch_id == "settings-propr-switch":
            self.app._propr_enabled = event.value
            self.app._set_status(f"Propr dry-run {'ON' if event.value else 'OFF'}")
            self.app._save_settings()
        elif switch_id.startswith("autoref-"):
            tab_id = switch_id.removeprefix("autoref-")
            self.app._set_auto_refresh_tab(tab_id, event.value)

    def action_dismiss(self, result=None) -> None:
        self.app.pop_screen()


class PaperCalendarScreen(ModalScreen):
    """Day-by-day net $ P&L for the paper-trading ledger, grouped by EXIT
    date (when a trade's outcome actually happened) -- the same shape a real
    exchange's own PNL calendar uses. A still-open trade doesn't appear on
    any day yet; it only lands once it resolves."""

    BINDINGS = [("escape", "dismiss", "Close"), ("c", "dismiss", "Close")]

    def __init__(self) -> None:
        super().__init__()
        now = datetime.now(timezone.utc)
        self._year = now.year
        self._month = now.month

    def compose(self) -> ComposeResult:
        with Vertical(id="calendar-panel"):
            yield Label("PAPER TRADING CALENDAR", id="calendar-title")
            yield Static("", id="calendar-summary")
            with Horizontal(id="calendar-nav"):
                yield Button("< Prev", id="calendar-prev-btn")
                yield Label("", id="calendar-month-label")
                yield Button("Next >", id="calendar-next-btn")
            yield Static("", id="calendar-grid")
            yield Label(
                "Grouped by exit date, not entry date -- a trade only appears once it resolves. "
                "Press Esc or c to close.",
                id="calendar-close-hint",
            )

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        data = get_daily_pnl(self._year, self._month)

        self.query_one("#calendar-month-label", Label).update(
            f"{calendar_mod.month_name[self._month]} {self._year}"
        )

        summary = Text()
        summary.append("Total Profit ", style="#8b949e")
        summary.append(f"${data.total_profit:.2f}", style=_LONG_STYLE)
        summary.append("    Total Loss ", style="#8b949e")
        summary.append(f"${data.total_loss:.2f}", style=_SHORT_STYLE)
        summary.append("    Net ", style="#8b949e")
        summary.append(f"${data.net:+.2f}", style=_LONG_STYLE if data.net >= 0 else _SHORT_STYLE)
        self.query_one("#calendar-summary", Static).update(summary)

        self.query_one("#calendar-grid", Static).update(self._build_grid(data.daily))

    def _build_grid(self, daily: dict[int, float]) -> Table:
        table = Table(show_header=True, header_style="bold #e3b341", box=None, padding=(0, 1), expand=True)
        for name in ("S", "M", "T", "W", "T", "F", "S"):
            table.add_column(name, justify="center")
        weeks = calendar_mod.Calendar(firstweekday=6).monthdayscalendar(self._year, self._month)
        for week in weeks:
            cells = []
            for day in week:
                if day == 0:
                    cells.append("")
                    continue
                pnl = daily.get(day)
                if pnl is None:
                    cells.append(str(day))
                else:
                    color = "#3fb950" if pnl >= 0 else "#f85149"
                    sign = "+" if pnl >= 0 else ""
                    cells.append(f"[bold]{day}[/bold]\n[{color}]{sign}{pnl:.2f}[/{color}]")
            table.add_row(*cells)
        return table

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "calendar-prev-btn":
            self._month -= 1
            if self._month < 1:
                self._month, self._year = 12, self._year - 1
            self._refresh()
        elif event.button.id == "calendar-next-btn":
            self._month += 1
            if self._month > 12:
                self._month, self._year = 1, self._year + 1
            self._refresh()

    def action_dismiss(self, result=None) -> None:
        self.app.pop_screen()


_IMPACT_STYLE = {"high": "bold #f85149", "medium": "bold #e3b341", "low": "#6e7681"}


def _impact_text(impact: str) -> Text:
    return Text(impact.upper(), style=_IMPACT_STYLE.get(impact, "#6e7681"))


class NewsDetailScreen(ModalScreen):
    """Full headline + source + link for one news row -- pushed on click/Enter
    from the News tab's table, since a DataTable cell can't show a wrapped
    headline or a clickable link inline.

    The link is shown as a clickable OSC8 hyperlink (works in terminals that
    support it, no ctrl/cmd needed in most -- that's a terminal-specific
    convention, not something this app controls), but since support is
    inconsistent, "Copy Link" / the c key is the reliable path: it copies the
    URL to the system clipboard via OSC52, which considerably more terminals
    support than clickable OSC8 links, so it works even when the link itself
    doesn't open on click.
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("n", "dismiss", "Close"),
        ("c", "copy_link", "Copy link"),
    ]

    def __init__(self, item) -> None:
        super().__init__()
        self._item = item

    def compose(self) -> ComposeResult:
        n = self._item
        published = n.published.strftime("%Y-%m-%d %H:%M UTC") if n.published else "time unknown"
        with Vertical(id="news-detail-panel"):
            yield Label("NEWS", id="news-detail-title")
            with VerticalScroll(id="news-detail-body"):
                yield Static(Text(n.title, style="bold"), id="news-detail-headline")
                meta = Text()
                meta.append(n.source, style="#8b949e")
                meta.append("   ")
                meta.append(published, style="#6e7681")
                meta.append("   ")
                meta.append_text(_impact_text(n.impact))
                yield Static(meta, id="news-detail-meta")
                link_text = Text.from_markup(f"[underline link={n.url}]{n.url}[/underline link]")
                yield Static(link_text, id="news-detail-link")
            with Horizontal(id="news-detail-actions"):
                yield Button("Copy Link (c)", id="news-detail-copy-btn")
                yield Button("Close (Esc)", id="news-detail-close-btn")

    def action_dismiss(self, result=None) -> None:
        self.app.pop_screen()

    def action_copy_link(self) -> None:
        self.app.copy_to_clipboard(self._item.url)
        self.app.notify("Link copied to clipboard", timeout=3)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "news-detail-copy-btn":
            self.action_copy_link()
        elif event.button.id == "news-detail-close-btn":
            self.action_dismiss()


class PaperTradeDetailScreen(ModalScreen):
    """Full detail for one paper trade -- entry/stop/all three targets, which
    level was actually reached, and the original signal's own reason string.
    Pushed on click/Enter from the Paper tab's table, since the table itself
    drops the Reason column to keep rows scannable."""

    BINDINGS = [("escape", "dismiss", "Close"), ("p", "dismiss", "Close")]

    def __init__(self, trade) -> None:
        super().__init__()
        self._trade = trade

    def compose(self) -> ComposeResult:
        t = self._trade
        dir_style = _LONG_STYLE if t.direction == "long" else _SHORT_STYLE
        with Vertical(id="paper-detail-panel"):
            yield Label("PAPER TRADE", id="paper-detail-title")
            with VerticalScroll(id="paper-detail-body"):
                header = Text()
                header.append(t.symbol, style="bold")
                header.append("  ")
                header.append(t.direction.upper(), style=dir_style)
                header.append(f"  ({t.market}, {t.timeframe})", style="#6e7681")
                yield Static(header, id="paper-detail-header")

                status_style = (
                    _LONG_STYLE if t.status == "won" else (_SHORT_STYLE if t.status == "lost" else _DIM_STYLE)
                )
                status_line = Text()
                status_line.append(f"{t.status.upper()}", style=status_style)
                if t.best_level:
                    status_line.append(f"  (reached TP{t.best_level} of {len(t.targets)})", style="#8b949e")
                if not t.closed and t.status == "won":
                    status_line.append("  -- still tracking for a higher target", style="#6e7681")
                yield Static(status_line, id="paper-detail-status")

                levels = Text()
                levels.append(f"Entry {t.entry_price:.6g}", style=_ENTRY_STYLE)
                levels.append("   ")
                levels.append(f"Stop {t.stop_loss:.6g}", style=_STOP_STYLE)
                yield Static(levels, id="paper-detail-entry")

                tp_line = Text()
                for i, tp in enumerate(t.targets, start=1):
                    reached = t.best_level >= i
                    style = _TARGET_STYLE if reached else "#6e7681"
                    tp_line.append(f"TP{i} {tp:.6g}{'  (hit)' if reached else ''}   ", style=style)
                yield Static(tp_line, id="paper-detail-targets")

                if t.exit_price is not None:
                    exit_line = Text()
                    exit_line.append(
                        f"Exit {t.exit_price:.6g} ({t.exit_reason}) at "
                        f"{t.exit_time.strftime('%Y-%m-%d %H:%M') if t.exit_time else '?'}",
                        style="#8b949e",
                    )
                    yield Static(exit_line, id="paper-detail-exit")

                result_line = Text()
                if t.r_multiple is not None:
                    r_style = _LONG_STYLE if t.r_multiple > 0 else (_SHORT_STYLE if t.r_multiple < 0 else _DIM_STYLE)
                    result_line.append(f"{t.r_multiple:+.2f}R", style=r_style)
                    result_line.append("   ")
                    result_line.append(f"${t.pnl_usd:+.2f}", style=r_style)
                else:
                    result_line.append("Not yet resolved", style="#6e7681")
                yield Static(result_line, id="paper-detail-result")

                sizing = Text(
                    f"${t.notional_usd:.0f} margin x {t.leverage:.0f}x leverage = ${t.position_usd:.0f} exposure",
                    style="#6e7681",
                )
                yield Static(sizing, id="paper-detail-sizing")

                yield Static(
                    Text(f"Entered {t.entry_time.strftime('%Y-%m-%d %H:%M UTC')}", style="#6e7681"),
                    id="paper-detail-entrytime",
                )
                yield Static(Text(t.reason or "(no reason recorded)", style="italic"), id="paper-detail-reason")
            yield Label("Press Esc or p to close", id="paper-detail-close-hint")

    def action_dismiss(self, result=None) -> None:
        self.app.pop_screen()


def _glossary_as_markdown() -> str:
    from tradingagents.scanner.glossary import GLOSSARY

    lines = []
    for term, definition in GLOSSARY.items():
        lines.append(f"### {term}\n{definition}\n")
    return "\n".join(lines)


class ScannerApp(App):
    """Live scanner dashboard. Every tab is independently refreshable and lazy-loaded."""

    CSS_PATH = str(CSS_FILE)
    TITLE = "TradingAgents Scanner"

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh_active", "Refresh"),
        ("g", "show_glossary", "Glossary"),
        ("h", "show_howto", "How-To"),
        ("s", "show_settings", "Settings"),
        ("c", "show_calendar", "Calendar"),
        ("a", "toggle_auto", "Auto-refresh"),
        ("f", "toggle_favorite", "Favorite"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._market = "crypto"
        self._timeframe = "1h"
        self._auto_refresh = False
        self._loaded: set[str] = set()
        self._scanning: set[str] = set()
        self._auto_timers: dict[str, object] = {}
        self._last_results: dict[str, list] = {}
        self._table_refilled_at: dict[str, float] = {}
        self._favorites: dict[str, dict] = load_favorites()
        self._news_category = "crypto"
        _settings = load_settings()
        self._paper_leverage = _settings["paper_leverage"]
        self._paper_notional = _settings["paper_notional"]
        self._testnet_enabled = _settings["testnet_enabled"] and credentials_configured()
        self._propr_enabled = _settings["propr_enabled"] and propr_credentials_configured()
        _saved_auto_tabs = _settings.get("auto_refresh_tabs")
        _all_tab_ids = {tab_id for tab_id, _ in AUTO_REFRESH_TABS}
        if _saved_auto_tabs is None:
            self._auto_refresh_tabs: set[str] = set(_all_tab_ids)
        else:
            enabled = set(_saved_auto_tabs)
            # A tab added to AUTO_REFRESH_TABS after this settings.json was last
            # saved (Structure, Propr, ... whatever comes next) is absent from
            # both lists below -- that's "never seen", not "turned off", so it
            # defaults on. auto_refresh_tabs_seen is the saved snapshot of every
            # tab id Settings has ever shown a switch for; a legacy settings.json
            # from before this field existed has no seen-list, so we fall back to
            # treating the enabled list itself as what was seen -- meaning any tab
            # not in it (old or new) is unseen and defaults on, same outcome as if
            # every one of today's existing tabs had always been explicitly on.
            _seen = _settings.get("auto_refresh_tabs_seen")
            seen = set(_seen) if _seen is not None else set(enabled)
            self._auto_refresh_tabs = enabled | (_all_tab_ids - seen)

    def _symbol_cell(self, symbol: str) -> Text:
        """Symbol cell with a gold star prefix if it's favorited -- same helper
        used everywhere a Symbol column is rendered, so toggling a favorite and
        re-rendering from cached results looks identical everywhere."""
        text = Text()
        if symbol in self._favorites:
            text.append("★ ", style=_FAV_STAR_STYLE)
        else:
            text.append("  ")
        text.append(symbol)
        return text

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="control-bar"):
            yield Label("Market")
            yield Select(MARKETS, value="crypto", id="market-select", allow_blank=False)
            yield Label("Timeframe")
            yield Select(TIMEFRAMES, value="1h", id="timeframe-select", allow_blank=False)
            yield Button("Scan Now", id="scan-now-btn")
            yield Label("Auto-refresh")
            yield Switch(value=False, id="auto-switch")
            yield Static("", id="auto-count")
            yield Static("", id="status-line")
        yield Static("Market regime: loading...", id="regime-banner")

        with TabbedContent(initial="tab-confirmed"):
            with TabPane("Confirmed", id="tab-confirmed"):
                yield Label(
                    "Closed-candle, volume-confirmed breakouts/breakdowns. Not a prediction -- a candidate list.",
                    classes="panel-subtitle",
                )
                yield Label("CONFIRMED LONG (breakout)", classes="panel-title")
                yield DataTable(id="table-confirmed-long", zebra_stripes=True)
                yield Label("CONFIRMED SHORT (breakdown)", classes="panel-title")
                yield DataTable(id="table-confirmed-short", zebra_stripes=True)
            with TabPane("Approaching", id="tab-approaching"):
                yield Label(
                    "Mid-candle, still-forming setups. Can vanish before the bar closes -- a watchlist, not a signal.",
                    classes="panel-subtitle",
                )
                yield Label("APPROACHING LONG (unconfirmed)", classes="panel-title")
                yield DataTable(id="table-approaching-long", zebra_stripes=True)
                yield Label("APPROACHING SHORT (unconfirmed)", classes="panel-title")
                yield DataTable(id="table-approaching-short", zebra_stripes=True)
            with TabPane("Pullback", id="tab-pullback"):
                yield Label("PULLBACK LONG (continuation)", classes="panel-title")
                yield DataTable(id="table-pullback-long", zebra_stripes=True)
                yield Label("PULLBACK SHORT (reversal)", classes="panel-title")
                yield DataTable(id="table-pullback-short", zebra_stripes=True)
            with TabPane("Accumulation", id="tab-accumulation"):
                yield Label("ACCUMULATION (watchlist -- not broken out yet)", classes="panel-title")
                yield Label(
                    "Basing in a tight range. Most bases never break out at all -- this is not bullish by itself.",
                    classes="panel-subtitle",
                )
                yield DataTable(id="table-accumulation", zebra_stripes=True)
            with TabPane("Structure", id="tab-structure"):
                with VerticalScroll(id="structure-scroll"):
                    yield Label(
                        "Swing-structure breaks (BOS/CHoCH) -- a separate detector from Confirmed/Approaching's "
                        "N-bar high/low breakout; can agree or disagree with those tables. Backtested: roughly "
                        "flat expectancy overall, and the score does not reliably rank quality yet -- treat as "
                        "exploratory, not a sized-trade signal.",
                        classes="panel-subtitle",
                    )
                    yield Label("CONFIRMED STRUCTURE BREAK LONG", classes="panel-title")
                    yield DataTable(id="table-smc-long", zebra_stripes=True, classes="structure-table")
                    yield Label("CONFIRMED STRUCTURE BREAK SHORT", classes="panel-title")
                    yield DataTable(id="table-smc-short", zebra_stripes=True, classes="structure-table")
                    yield Label(
                        "APPROACHING STRUCTURE LEVEL -- mid-candle, testing/nearing a swing high/low that would "
                        "confirm a BOS or CHoCH if it holds. Can vanish before the bar closes.",
                        classes="panel-subtitle",
                    )
                    yield DataTable(id="table-smc-approaching-long", zebra_stripes=True, classes="structure-table")
                    yield DataTable(id="table-smc-approaching-short", zebra_stripes=True, classes="structure-table")
                    yield Label(
                        "ZONE RETEST -- price dipped into a still-ACTIVE liquidity zone (never broken) and "
                        "closed back out, holding it. Different from the break tables above: this fires on a "
                        "zone HOLDING, not breaking.",
                        classes="panel-subtitle",
                    )
                    yield Label("ZONE RETEST LONG (bounced off support)", classes="panel-title")
                    yield DataTable(id="table-zone-retest-long", zebra_stripes=True, classes="structure-table")
                    yield Label("ZONE RETEST SHORT (rejected at resistance)", classes="panel-title")
                    yield DataTable(id="table-zone-retest-short", zebra_stripes=True, classes="structure-table")
            with TabPane("Backtest", id="tab-backtest"):
                with Horizontal(id="backtest-controls"):
                    yield Label("Symbol")
                    yield Input(value="BTC/USDT:USDT", id="backtest-symbol-input", placeholder="e.g. BTC/USDT:USDT or AAPL")
                    yield Label("Strategy")
                    yield Select(
                        [("Breakout", "breakout"), ("Pullback", "pullback")],
                        value="breakout", id="backtest-strategy-select", allow_blank=False,
                    )
                    yield Label("Bars")
                    yield Select(
                        [("500", "500"), ("1000", "1000"), ("2000", "2000")],
                        value="1000", id="backtest-bars-select", allow_blank=False,
                    )
                    yield Button("Run Backtest", id="backtest-run-btn")
                yield Label(
                    "Replays the Breakout/Pullback detector bar-by-bar over real history (uses the "
                    "current Market above). One target (TP1) per trade, not the full staged ladder -- "
                    "a read on whether this detector has edge here, not an exact P&L simulation.",
                    classes="panel-subtitle",
                )
                yield Static("Pick a symbol and press Run Backtest.", id="backtest-summary")
                yield DataTable(id="table-backtest", zebra_stripes=True)
            with TabPane("News", id="tab-news"):
                with Horizontal(id="news-controls"):
                    yield Label("Category")
                    yield Select(
                        [("Crypto", "crypto"), ("Stocks", "stocks")],
                        value="crypto", id="news-category-select", allow_blank=False,
                    )
                    yield Button("Refresh", id="news-refresh-btn")
                yield Label(
                    "Via public RSS (CoinDesk/Cointelegraph for crypto, CNBC for stocks) -- no API key. "
                    "Impact is a rough keyword heuristic (forex-factory-style HIGH/MEDIUM/LOW), not a "
                    "real event calendar -- high-impact floats to the top. Click/Enter a row for the "
                    "full headline and a link to the source.",
                    classes="panel-subtitle",
                )
                yield DataTable(id="table-news", zebra_stripes=True, cursor_type="row")
            with TabPane("Paper", id="tab-paper"):
                yield Static("Paper trading: loading...", id="paper-summary")
                yield Label(
                    "Every CONFIRMED hit gets logged here automatically (whichever Market/Timeframe "
                    "you were scanning at the time) and re-checked against real price action on each "
                    "refresh. Entry Time is the candle's OPEN time (standard exchange convention), not "
                    "when it closed -- a scan right after 6pm shows entry 5pm, since that's the bar that "
                    "just finished. A win stays open past TP1 to chase TP2/TP3. Margin/leverage per trade "
                    "are set in Settings (press s) and locked in per-trade at log time. Click/Enter a row "
                    "for the full reason and detail, or press c for a day-by-day P&L calendar.",
                    classes="panel-subtitle",
                )
                yield DataTable(id="table-paper", zebra_stripes=True, cursor_type="row")
            with TabPane("Testnet", id="tab-testnet"):
                yield Static("Testnet auto-execution: checking...", id="testnet-summary")
                yield Label(
                    "Real orders on Binance Futures TESTNET (fake funds, real matching engine) -- "
                    "turned on/off in Settings (press s). Entry is a market order; Stop/TP1 are "
                    "reduce-only. Only CONFIRMED crypto signals are eligible. This is a log of what "
                    "was actually sent, not a simulation.",
                    classes="panel-subtitle",
                )
                yield DataTable(id="table-testnet", zebra_stripes=True)
            with TabPane("Propr", id="tab-propr"):
                yield Static("Propr dry-run: checking...", id="propr-summary")
                yield Label(
                    "DRY RUN ONLY -- logs the exact order a Confirmed signal would place on your real "
                    "Propr challenge account, never sends it. There is no separate test account on "
                    "Propr; every order here is just a log entry, resolved against live Hyperliquid "
                    "mark price. Many signals show 'untradable' -- most small/mid-caps (e.g. Q, QNT) "
                    "aren't listed on Hyperliquid at all. Crypto leverage caps at 2x by default "
                    "(10x only for BTC/ETH/SOL), well below Binance.",
                    classes="panel-subtitle",
                )
                yield DataTable(id="table-propr", zebra_stripes=True)
            with TabPane("Favorites", id="tab-favorites"):
                yield Label("FAVORITES", classes="panel-title")
                yield Label(
                    "Press f on any row (in any tab) to favorite/unfavorite it. Press f here to remove.",
                    classes="panel-subtitle",
                )
                yield DataTable(id="table-favorites", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        for table_id, columns in self._table_specs().items():
            table = self.query_one(f"#{table_id}", DataTable)
            table.add_columns(*columns)
        self._fill_favorites_table()
        self._scan_tab("tab-confirmed")
        self._run_regime()
        self.set_interval(900, self._run_regime)  # independent of any tab/timeframe -- a standing 15-min gauge

    def _table_specs(self) -> dict[str, list[str]]:
        confirmed_cols = ["Symbol", "Score", "Vol x", "RSI", "Entry (mkt)", "Entry (retest)", "Stop", "TP1", "TP2", "TP3", "Reason"]
        approaching_cols = ["Symbol", "Status", "Dist%", "Vol pace", "Bar%", "RSI", "Reason"]
        smc_cols = ["Symbol", "Structure", "Score", "Vol x", "Level", "Entry", "Stop", "Target", "Held", "Touches", "Pool", "Reason"]
        smc_approaching_cols = ["Symbol", "Status", "Would be", "Dist%", "Level", "Close", "Held", "Reason"]
        zone_retest_cols = ["Symbol", "Score", "Vol x", "Zone", "Entry", "Stop", "Target", "Touches", "Reason"]
        return {
            "table-confirmed-long": confirmed_cols,
            "table-confirmed-short": confirmed_cols,
            "table-smc-long": smc_cols,
            "table-smc-short": smc_cols,
            "table-approaching-long": approaching_cols,
            "table-approaching-short": approaching_cols,
            "table-smc-approaching-long": smc_approaching_cols,
            "table-smc-approaching-short": smc_approaching_cols,
            "table-zone-retest-long": zone_retest_cols,
            "table-zone-retest-short": zone_retest_cols,
            "table-pullback-long": ["Symbol", "Score", "Retrace%", "Vol x", "Entry", "Stop", "T1", "T2", "Reason"],
            "table-pullback-short": ["Symbol", "Score", "Retrace%", "Vol x", "Entry", "Stop", "T1", "T2", "Reason"],
            "table-accumulation": ["Symbol", "Score", "Range%", "ATR%", "Pos", "Reason"],
            "table-backtest": ["Dir", "Entry Time", "Entry", "Exit Time", "Exit", "Reason", "R"],
            "table-news": ["Time", "Impact", "Source", "Headline"],
            "table-paper": [
                "Symbol", "Market", "TF", "Dir", "Status", "TPs Hit", "Entry Time", "Entry",
                "Stop", "Exit", "R", "$PnL",
            ],
            "table-testnet": [
                "Symbol", "TF", "Dir", "Qty", "Status", "Entry", "Stop", "TP", "Exit", "R", "$PnL", "Created", "Error",
            ],
            "table-propr": [
                "Symbol", "Base", "TF", "Dir", "Qty", "Lev", "Status", "Entry", "Stop", "TP", "Exit",
                "R", "$PnL", "Created",
            ],
            "table-favorites": ["Symbol", "Market", "Added"],
        }

    # ---- control bar handlers ----

    def on_select_changed(self, event: Select.Changed) -> None:
        # Textual's Select fires Changed on initial mount too, even though
        # nothing actually changed from the compose()-time default. Without
        # this guard, that spurious event clears _loaded right as the
        # startup scan is running, and can race the _scanning guard in
        # _scan_tab such that the tab never gets back into _loaded even
        # after it finishes -- silently excluding it from auto-refresh and
        # making every future visit re-scan it as if new. Only real changes
        # (value differs from current state) should do anything.
        if event.select.id == "market-select":
            if event.value == self._market:
                return
            self._market = event.value
            if self._market in _NO_4H_MARKETS and self._timeframe == "4h":
                self._timeframe = "1d"
                self.query_one("#timeframe-select", Select).value = "1d"
                self._set_status("Real Stocks has no 4h bar -- switched timeframe to 1d.")
            self._loaded.clear()  # universe changed -- stale data everywhere
            self._restart_auto_timer()  # old tabs' timers are stale too
            self._refresh_active_tab()
        elif event.select.id == "timeframe-select":
            if event.value == self._timeframe:
                return
            if event.value == "4h" and self._market in _NO_4H_MARKETS:
                self._set_status("Real Stocks has no 4h bar (yfinance limitation) -- pick 15m, 1h, or 1d.")
                self.query_one("#timeframe-select", Select).value = self._timeframe  # revert the UI
                return
            self._timeframe = event.value
            self._loaded.clear()
            self._restart_auto_timer()
            self._refresh_active_tab()

        elif event.select.id == "news-category-select":
            if event.value == self._news_category:
                return
            self._news_category = event.value
            self._open_news_tab(force=True)

        elif event.select.id == "settings-leverage-select":
            new_leverage = float(event.value)
            if new_leverage == self._paper_leverage:
                return
            self._paper_leverage = new_leverage
            self._save_settings()

        elif event.select.id == "settings-notional-select":
            new_notional = float(event.value)
            if new_notional == self._paper_notional:
                return
            self._paper_notional = new_notional
            self._save_settings()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "scan-now-btn":
            self._scan_tab(self._active_tab_id(), force=True)
        elif event.button.id == "backtest-run-btn":
            self._start_backtest()
        elif event.button.id == "news-refresh-btn":
            self._open_news_tab(force=True)

    def _refresh_active_tab(self) -> None:
        """Same dispatch on_tabbed_content_tab_activated uses, for when the
        Market/Timeframe controls (not a tab click) are what just went stale."""
        active = self._active_tab_id()
        if active not in ("tab-favorites", "tab-backtest", "tab-news", "tab-paper", "tab-testnet", "tab-propr"):
            self._scan_tab(active)

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "auto-switch":
            self._auto_refresh = event.value
            self._restart_auto_timer()

    # DataTable.clear() always snaps the cursor back to (0, 0), which fires
    # this same CellHighlighted event a real click would -- without this
    # guard, every scan (auto or manual) would silently overwrite the
    # clipboard with whatever now sits at (0, 0), with no click involved.
    _REFILL_SUPPRESS_SECONDS = 1.5

    def on_data_table_cell_highlighted(self, event: DataTable.CellHighlighted) -> None:
        """Click (or arrow-key onto) any cell in any cell-cursor table -- every
        signal table defaults to cursor_type="cell" -- and its text is on the
        clipboard immediately, ready to paste into TradingView's paper-trading
        order fields (Entry/Stop/TP/etc.). Tables that use cursor_type="row"
        (News, Paper) never fire this event, so their own row-click/detail-
        screen behavior is untouched. Via OSC 52, same as the existing
        Copy Link action on a news item -- needs a terminal that supports it."""
        table_id = event.data_table.id
        refilled_at = self._table_refilled_at.get(table_id or "")
        if refilled_at is not None and time.time() - refilled_at < self._REFILL_SUPPRESS_SECONDS:
            return
        value = event.value
        text = value.plain if isinstance(value, Text) else str(value)
        text = text.strip().lstrip("★").strip()
        if not text:
            return
        self.copy_to_clipboard(text)
        self.notify(f'Copied "{text}"', timeout=1.5)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "table-news":
            index = event.row_key.value
            items = self._last_results.get("tab-news", [])
            if index is not None and 0 <= int(index) < len(items):
                self.push_screen(NewsDetailScreen(items[int(index)]))
        elif event.data_table.id == "table-paper":
            trade_id = event.row_key.value
            trades = self._last_results.get("tab-paper", [])
            trade = next((t for t in trades if str(t.id) == str(trade_id)), None)
            if trade is not None:
                self.push_screen(PaperTradeDetailScreen(trade))

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        tab_id = event.tab.id or ""
        # Tabs/TabPane ids differ by a "--content-tab-" prefix internally; match by suffix.
        known_tabs = (
            "tab-confirmed", "tab-approaching", "tab-pullback", "tab-accumulation", "tab-structure",
            "tab-backtest", "tab-news", "tab-paper", "tab-testnet", "tab-propr", "tab-favorites",
        )
        for known in known_tabs:
            if known in tab_id:
                if known == "tab-news":
                    if "tab-news" not in self._loaded:
                        self._open_news_tab()
                elif known == "tab-paper":
                    if "tab-paper" not in self._loaded:
                        self._open_paper_tab()
                elif known == "tab-testnet":
                    if "tab-testnet" not in self._loaded:
                        self._open_testnet_tab()
                elif known == "tab-propr":
                    if "tab-propr" not in self._loaded:
                        self._open_propr_tab()
                elif known in ("tab-favorites", "tab-backtest"):
                    pass  # favorites is static local data; backtest only runs on Run Backtest
                elif known not in self._loaded:
                    self._scan_tab(known)
                break

    def _active_tab_id(self) -> str:
        tabbed = self.query_one(TabbedContent)
        return tabbed.active or "tab-confirmed"

    # ---- actions (keybindings) ----

    def action_refresh_active(self) -> None:
        self._scan_tab(self._active_tab_id(), force=True)

    def action_show_glossary(self) -> None:
        self.push_screen(GlossaryScreen())

    def action_show_howto(self) -> None:
        self.push_screen(HowToScreen())

    def action_show_settings(self) -> None:
        self.push_screen(SettingsScreen(
            leverage=self._paper_leverage, notional=self._paper_notional, testnet_enabled=self._testnet_enabled,
            propr_enabled=self._propr_enabled,
        ))

    def action_show_calendar(self) -> None:
        self.push_screen(PaperCalendarScreen())

    def _save_settings(self) -> None:
        save_settings({
            "paper_leverage": self._paper_leverage,
            "paper_notional": self._paper_notional,
            "testnet_enabled": self._testnet_enabled,
            "propr_enabled": self._propr_enabled,
            "auto_refresh_tabs": sorted(self._auto_refresh_tabs),
            "auto_refresh_tabs_seen": sorted(tab_id for tab_id, _ in AUTO_REFRESH_TABS),
        })

    def _set_auto_refresh_tab(self, tab_id: str, enabled: bool) -> None:
        """Opt one tab in/out of the auto-refresh rotation, live -- takes
        effect immediately, not just on the next full restart."""
        if enabled:
            self._auto_refresh_tabs.add(tab_id)
            if self._auto_refresh and tab_id in self._loaded and tab_id not in self._auto_timers:
                self._start_auto_timer_for(tab_id)
        else:
            self._auto_refresh_tabs.discard(tab_id)
            timer = self._auto_timers.pop(tab_id, None)
            if timer is not None:
                timer.stop()
        self._update_auto_label()
        self._save_settings()

    def action_toggle_favorite(self) -> None:
        focused = self.focused
        if not isinstance(focused, DataTable) or focused.row_count == 0:
            return
        try:
            cell_key = focused.coordinate_to_cell_key(focused.cursor_coordinate)
        except Exception:
            return
        symbol = cell_key.row_key.value
        if symbol is None:
            return
        self._toggle_favorite(str(symbol))

    def action_toggle_auto(self) -> None:
        switch = self.query_one("#auto-switch", Switch)
        switch.value = not switch.value  # triggers on_switch_changed

    # ---- favorites ----

    def _toggle_favorite(self, symbol: str) -> None:
        if symbol in self._favorites:
            del self._favorites[symbol]
        else:
            self._favorites[symbol] = {
                "market": self._market,
                "added": datetime.now().isoformat(timespec="seconds"),
            }
        save_favorites(self._favorites)
        self._fill_favorites_table()
        self._rerender_cached_tabs()

    def _fill_favorites_table(self) -> None:
        table = self.query_one("#table-favorites", DataTable)
        self._clear_table(table)
        for symbol, info in sorted(self._favorites.items()):
            table.add_row(symbol, info.get("market", "?"), info.get("added", "?"), key=symbol)

    def _rerender_cached_tabs(self) -> None:
        """Re-render every tab from its cached results (no network call) so
        favorite stars update everywhere immediately, not just where f was pressed."""
        fillers = {
            "tab-confirmed": self._fill_confirmed,
            "tab-approaching": self._fill_approaching,
            "tab-pullback": self._fill_pullback,
            "tab-accumulation": self._fill_accumulation,
            "tab-smc": self._fill_smc,
            "tab-smc-approaching": self._fill_smc_approaching,
            "tab-zone-retest": self._fill_zone_retest,
        }
        for tab_id, filler in fillers.items():
            if tab_id in self._last_results:
                filler(self._last_results[tab_id])

    # ---- auto-refresh: every LOADED tab refreshes independently, aligned to
    # its own candle close -- not just whichever tab happens to be on screen. ----

    def _timeframe_for(self, tab_id: str) -> str:
        if tab_id == "tab-accumulation" and self._timeframe == "15m":
            # a multi-day base can't be read on 15m bars; Real Stocks has no
            # 4h, so it goes straight to 1d instead (scan_accumulation does
            # the same substitution server-side for direct API callers).
            return "1d" if self._market in _NO_4H_MARKETS else "4h"
        return self._timeframe

    def _restart_auto_timer(self) -> None:
        for timer in self._auto_timers.values():
            timer.stop()
        self._auto_timers.clear()
        if not self._auto_refresh:
            self._update_auto_label()
            return
        for tab_id in self._loaded:
            if tab_id in self._auto_refresh_tabs:
                self._start_auto_timer_for(tab_id)
        self._update_auto_label()

    # These tabs don't need to wait for a full candle close to be worth
    # re-checking, each for its own reason, and none of them feeds paper
    # trading or testnet execution on their own (only Confirmed's results
    # do, via _log_paper_trades):
    #   - Approaching reads the live forming candle directly.
    #   - Paper/Testnet re-check ALREADY-ENTERED trades against fresh
    #     price via resolve_open_trades()/resolve_testnet_orders() --
    #     these correctly catch an intra-candle stop/TP wick off that
    #     candle's high/low, but only find out about it whenever they
    #     next run. A stop hit 15 minutes after entry was otherwise only
    #     discovered up to an hour late, at the next full-candle refresh.
    #   - Structure's approaching-half reads the live candle too (same as
    #     Approaching); its confirmed-half just gets re-checked a bit more
    #     often than strictly needed, which is harmless -- same answer
    #     until a candle genuinely closes.
    # All are capped at a 15-min cadence, giving 15/30/45-min checkpoints
    # within a 1h/4h/1d candle. The checkpoint that would land exactly on
    # the candle-close boundary itself is skipped: that moment is already
    # covered by the normal full-candle refresh (Confirmed and everything
    # else re-scans right then), so a duplicate check at the same instant
    # adds nothing.
    _FAST_REFRESH_TABS = {"tab-approaching", "tab-paper", "tab-testnet", "tab-structure", "tab-propr"}
    _FAST_REFRESH_INTERVAL_SECONDS = 900

    def _start_auto_timer_for(self, tab_id: str) -> None:
        try:
            candle_seconds = timeframe_to_seconds(self._timeframe_for(tab_id))
        except ValueError:
            candle_seconds = 3600
        seconds = candle_seconds
        fast = tab_id in self._FAST_REFRESH_TABS
        if fast:
            seconds = min(seconds, self._FAST_REFRESH_INTERVAL_SECONDS)
        now = time.time()
        next_close = (now // seconds + 1) * seconds
        if fast and seconds < candle_seconds and next_close % candle_seconds == 0:
            next_close += seconds  # skip the candle-close boundary, already covered elsewhere
        delay = max(5.0, next_close - now + 3)
        self._auto_timers[tab_id] = self.set_timer(delay, lambda t=tab_id: self._auto_refresh_fire(t))

    def _auto_refresh_fire(self, tab_id: str) -> None:
        if tab_id in self._FAST_REFRESH_TABS:
            _play_tone(_CHECKPOINT_TONE_HZ)  # a 15/30/45-min checkpoint, not a full candle close
        else:
            _play_tone(_TIMEFRAME_TONES_HZ.get(self._timeframe, 700))
        if tab_id == "tab-news":
            self._open_news_tab(force=True)
        elif tab_id == "tab-paper":
            self._open_paper_tab(force=True)
        elif tab_id == "tab-testnet":
            self._open_testnet_tab(force=True)
        elif tab_id == "tab-propr":
            self._open_propr_tab(force=True)
        else:
            self._scan_tab(tab_id, force=True)
        if self._auto_refresh and tab_id in self._auto_refresh_tabs:
            self._start_auto_timer_for(tab_id)  # re-arm just this one tab

    def _update_auto_label(self) -> None:
        label = self.query_one("#auto-count", Static)
        if self._auto_refresh:
            label.update(f"({len(self._auto_timers)} tab{'s' if len(self._auto_timers) != 1 else ''})")
        else:
            label.update("")

    # ---- scanning ----

    def _set_status(self, text: str, scanning: bool = False) -> None:
        status = self.query_one("#status-line", Static)
        status.update(text)
        status.set_class(scanning, "scanning")

    def _scan_tab(self, tab_id: str, force: bool = False) -> None:
        if tab_id in self._scanning:
            return
        if not force and tab_id in self._loaded:
            return
        self._loaded.add(tab_id)
        self._scanning.add(tab_id)
        self._set_status(f"Scanning {self._market} ({tab_id.replace('tab-', '')})...", scanning=True)
        if tab_id == "tab-confirmed":
            self._run_confirmed()
        elif tab_id == "tab-approaching":
            self._run_approaching()
        elif tab_id == "tab-pullback":
            self._run_pullback()
        elif tab_id == "tab-accumulation":
            self._run_accumulation()
        elif tab_id == "tab-structure":
            self._run_structure()

    @work(thread=True)
    def _run_confirmed(self) -> None:
        try:
            results = scan(timeframe=self._timeframe, market=self._market, top_n=25)
        except Exception as exc:
            self.call_from_thread(self._scan_failed, "tab-confirmed", str(exc))
            return
        self.call_from_thread(self._fill_confirmed, results)

    def _fill_confirmed(self, results) -> None:
        self._last_results["tab-confirmed"] = results
        long_table = self.query_one("#table-confirmed-long", DataTable)
        short_table = self.query_one("#table-confirmed-short", DataTable)
        self._clear_tables(long_table, short_table)
        for s in results:
            table = long_table if s.direction == "long" else short_table
            table.add_row(
                self._symbol_cell(s.symbol), f"{s.score:.2f}", _vol_text(s.volume_ratio), _rsi_text(s.rsi),
                Text(f"{s.close:.6g}", style=_ENTRY_STYLE), Text(f"{s.breakout_level:.6g}", style=_ENTRY_STYLE),
                Text(f"{s.stop_loss:.6g}", style=_STOP_STYLE),
                *[Text(f"{tp:.6g}", style=_TARGET_STYLE) for tp in s.take_profits],
                s.reason,
                key=s.symbol,
            )
        self._log_paper_trades(results)
        self._scan_done("tab-confirmed", len(results))

    @work(thread=True)
    def _run_structure(self) -> None:
        try:
            results = scan_smc(timeframe=self._timeframe, market=self._market, top_n=25)
            approaching_results = scan_smc_approaching(timeframe=self._timeframe, market=self._market, top_n=25)
            zone_results = scan_zone_retest(timeframe=self._timeframe, market=self._market, top_n=25)
        except Exception as exc:
            self.call_from_thread(self._scan_failed, "tab-structure", str(exc))
            return
        self.call_from_thread(self._fill_structure, results, approaching_results, zone_results)

    def _fill_structure(self, results, approaching_results, zone_results) -> None:
        self._fill_smc(results)
        self._fill_smc_approaching(approaching_results)
        self._fill_zone_retest(zone_results)
        self._scan_done("tab-structure", len(results) + len(approaching_results) + len(zone_results))

    def _fill_zone_retest(self, results) -> None:
        self._last_results["tab-zone-retest"] = results
        long_table = self.query_one("#table-zone-retest-long", DataTable)
        short_table = self.query_one("#table-zone-retest-short", DataTable)
        self._clear_tables(long_table, short_table)
        for s in results:
            table = long_table if s.direction == "long" else short_table
            table.add_row(
                self._symbol_cell(s.symbol), f"{s.score:.2f}", _vol_text(s.volume_ratio),
                Text(f"{s.zone_bottom:.6g}-{s.zone_top:.6g}", style=_DIM_STYLE),
                Text(f"{s.close:.6g}", style=_ENTRY_STYLE), Text(f"{s.stop_loss:.6g}", style=_STOP_STYLE),
                Text(f"{s.target:.6g}", style=_TARGET_STYLE), str(s.touches), s.reason,
                key=s.symbol,
            )

    def _fill_smc(self, results) -> None:
        self._last_results["tab-smc"] = results
        long_table = self.query_one("#table-smc-long", DataTable)
        short_table = self.query_one("#table-smc-short", DataTable)
        self._clear_tables(long_table, short_table)
        for s in results:
            table = long_table if s.direction == "long" else short_table
            structure_style = _LONG_STYLE if s.structure == "CHoCH" else _DIM_STYLE
            table.add_row(
                self._symbol_cell(s.symbol), Text(s.structure, style=structure_style), f"{s.score:.2f}",
                _vol_text(s.volume_ratio), Text(f"{s.level:.6g}", style=_ENTRY_STYLE),
                Text(f"{s.close:.6g}", style=_ENTRY_STYLE), Text(f"{s.stop_loss:.6g}", style=_STOP_STYLE),
                Text(f"{s.target:.6g}", style=_TARGET_STYLE), str(s.bars_held), str(s.touches),
                "yes" if s.swept_pool else "-", s.reason,
                key=s.symbol,
            )

    @work(thread=True)
    def _run_approaching(self) -> None:
        try:
            results = scan_approaching(timeframe=self._timeframe, market=self._market, top_n=25)
        except Exception as exc:
            self.call_from_thread(self._scan_failed, "tab-approaching", str(exc))
            return
        self.call_from_thread(self._fill_approaching, results)

    def _fill_approaching(self, results) -> None:
        self._last_results["tab-approaching"] = results
        long_table = self.query_one("#table-approaching-long", DataTable)
        short_table = self.query_one("#table-approaching-short", DataTable)
        self._clear_tables(long_table, short_table)
        for s in results:
            table = long_table if s.direction == "long" else short_table
            status_style = _LONG_STYLE if s.direction == "long" else _SHORT_STYLE
            status_text = Text(s.status, style=status_style if s.status == "testing" else _DIM_STYLE)
            table.add_row(
                self._symbol_cell(s.symbol), status_text, f"{s.distance_pct:.1f}", f"{s.volume_pace_ratio:.1f}x",
                f"{s.bar_elapsed_pct:.0f}%", _rsi_text(s.rsi), s.reason,
                key=s.symbol,
            )
        self._scan_done("tab-approaching", len(results))

    def _fill_smc_approaching(self, results) -> None:
        self._last_results["tab-smc-approaching"] = results
        long_table = self.query_one("#table-smc-approaching-long", DataTable)
        short_table = self.query_one("#table-smc-approaching-short", DataTable)
        self._clear_tables(long_table, short_table)
        for s in results:
            table = long_table if s.direction == "long" else short_table
            status_style = _LONG_STYLE if s.direction == "long" else _SHORT_STYLE
            status_text = Text(s.status, style=status_style if s.status == "testing" else _DIM_STYLE)
            would_be_style = _LONG_STYLE if s.would_be == "CHoCH" else _DIM_STYLE
            table.add_row(
                self._symbol_cell(s.symbol), status_text, Text(s.would_be, style=would_be_style),
                f"{s.distance_pct:.1f}", Text(f"{s.level:.6g}", style=_ENTRY_STYLE),
                Text(f"{s.close:.6g}", style=_ENTRY_STYLE), str(s.bars_held), s.reason,
                key=s.symbol,
            )

    @work(thread=True)
    def _run_pullback(self) -> None:
        try:
            results = scan_pullback(timeframe=self._timeframe, market=self._market, top_n=25)
        except Exception as exc:
            self.call_from_thread(self._scan_failed, "tab-pullback", str(exc))
            return
        self.call_from_thread(self._fill_pullback, results)

    def _fill_pullback(self, results) -> None:
        self._last_results["tab-pullback"] = results
        long_table = self.query_one("#table-pullback-long", DataTable)
        short_table = self.query_one("#table-pullback-short", DataTable)
        self._clear_tables(long_table, short_table)
        for s in results:
            table = long_table if s.direction == "long" else short_table
            table.add_row(
                self._symbol_cell(s.symbol), f"{s.score:.2f}", f"{s.retrace_pct:.1f}", _vol_text(s.volume_ratio),
                Text(f"{s.close:.6g}", style=_ENTRY_STYLE), Text(f"{s.stop_loss:.6g}", style=_STOP_STYLE),
                *[Text(f"{t:.6g}", style=_TARGET_STYLE) for t in s.targets],
                s.reason,
                key=s.symbol,
            )
        self._scan_done("tab-pullback", len(results))

    @work(thread=True)
    def _run_accumulation(self) -> None:
        try:
            results = scan_accumulation(
                timeframe=self._timeframe_for("tab-accumulation"),
                market=self._market, top_n=25,
            )
        except Exception as exc:
            self.call_from_thread(self._scan_failed, "tab-accumulation", str(exc))
            return
        self.call_from_thread(self._fill_accumulation, results)

    def _fill_accumulation(self, results) -> None:
        self._last_results["tab-accumulation"] = results
        table = self.query_one("#table-accumulation", DataTable)
        self._clear_table(table)
        for s in results:
            table.add_row(
                self._symbol_cell(s.symbol), f"{s.score:.2f}", f"{s.base_range_pct:.1f}", f"{s.atr_pct:.1f}",
                f"{s.position_in_base:.2f}", s.reason,
                key=s.symbol,
            )
        self._scan_done("tab-accumulation", len(results))

    # ---- backtest: purely manual, runs only on "Run Backtest" -- no lazy
    # load, no auto-refresh rotation (repeatedly re-running a backtest every
    # candle close would be pointless and expensive). ----

    def _start_backtest(self) -> None:
        if "tab-backtest" in self._scanning:
            return
        symbol = self.query_one("#backtest-symbol-input", Input).value.strip()
        if not symbol:
            self._set_status("Enter a symbol to backtest.")
            return
        strategy = self.query_one("#backtest-strategy-select", Select).value
        bars = int(self.query_one("#backtest-bars-select", Select).value)
        self._scanning.add("tab-backtest")
        self._set_status(f"Backtesting {symbol} ({strategy}, {bars} bars, {self._market})...", scanning=True)
        self._run_backtest(symbol, strategy, bars)

    @work(thread=True, exclusive=True, group="backtest")
    def _run_backtest(self, symbol: str, strategy: str, bars: int) -> None:
        try:
            summary = run_backtest(
                symbol, timeframe=self._timeframe, strategy=strategy, market=self._market, total_bars=bars,
            )
        except Exception as exc:
            self.call_from_thread(self._backtest_failed, str(exc))
            return
        self.call_from_thread(self._fill_backtest, summary)

    def _backtest_failed(self, error: str) -> None:
        self._scanning.discard("tab-backtest")
        self._set_status(f"Backtest failed: {error}")

    def _fill_backtest(self, summary) -> None:
        self._scanning.discard("tab-backtest")
        table = self.query_one("#table-backtest", DataTable)
        self._clear_table(table)
        for t in summary.trades:
            dir_text = Text("LONG", style=_LONG_STYLE) if t.direction == "long" else Text("SHORT", style=_SHORT_STYLE)
            r_style = _LONG_STYLE if t.r_multiple > 0 else (_SHORT_STYLE if t.r_multiple < 0 else _DIM_STYLE)
            table.add_row(
                dir_text,
                t.entry_time.strftime("%Y-%m-%d %H:%M"), f"{t.entry_price:.6g}",
                t.exit_time.strftime("%Y-%m-%d %H:%M"), f"{t.exit_price:.6g}",
                t.exit_reason, Text(f"{t.r_multiple:+.2f}", style=r_style),
            )
        summary_widget = self.query_one("#backtest-summary", Static)
        if summary.total == 0:
            summary_widget.update(
                f"{summary.symbol} -- {summary.strategy} on {summary.timeframe}, {summary.bars_tested} bars: "
                "no trades fired. Try more bars, the other strategy, or a more active symbol."
            )
        else:
            summary_widget.update(
                f"{summary.symbol} -- {summary.strategy} on {summary.timeframe} over {summary.bars_tested} bars: "
                f"{summary.total} trades, {summary.wins}W/{summary.losses}L ({summary.win_rate:.0f}% win rate), "
                f"avg {summary.avg_r:+.2f}R, total {summary.total_r:+.2f}R"
            )
        self._set_status(f"Backtest: {summary.total} trade(s) -- {self._clock()}")

    # ---- news: lazy-loaded, category switch forces a refetch ----

    def _open_news_tab(self, force: bool = False) -> None:
        # force=True (category switch, Refresh button) always proceeds, even
        # if a previous fetch is still in flight -- _run_news is exclusive,
        # so this just supersedes it rather than queuing behind it. Without
        # this, switching category while the initial load was still running
        # silently dropped the switch (the scanning guard returned early and
        # no second fetch ever ran).
        if not force:
            if "tab-news" in self._scanning or "tab-news" in self._loaded:
                return
        self._loaded.add("tab-news")
        self._scanning.add("tab-news")
        category = self.query_one("#news-category-select", Select).value
        self._set_status(f"Fetching {category} news...", scanning=True)
        self._run_news(category)

    @work(thread=True, exclusive=True, group="news")
    def _run_news(self, category: str) -> None:
        try:
            items = fetch_crypto_news(limit=40) if category == "crypto" else fetch_stock_news(limit=40)
        except Exception as exc:
            self.call_from_thread(self._scan_failed, "tab-news", str(exc))
            return
        self.call_from_thread(self._fill_news, items)

    def _fill_news(self, items) -> None:
        self._last_results["tab-news"] = items
        table = self.query_one("#table-news", DataTable)
        table.clear()
        for idx, n in enumerate(items):
            time_str = n.published.strftime("%m-%d %H:%M") if n.published else "?"
            # Keyed by list index, not URL -- a URL can legitimately repeat
            # (the same story syndicated under an identical link across two
            # feeds), and DataTable row keys must be unique or add_row raises.
            table.add_row(time_str, _impact_text(n.impact), n.source, n.title, key=str(idx))
        self._scan_done("tab-news", len(items))

    # ---- paper trading: every CONFIRMED hit is logged automatically (see
    # _fill_confirmed's call to _log_paper_trades); this tab just displays
    # the accumulating log and re-checks open trades against fresh prices. ----

    def _log_paper_trades(self, results) -> None:
        from datetime import timezone

        for s in results:
            if s.entry_time is None:
                continue
            entry_time = s.entry_time.to_pydatetime().replace(tzinfo=timezone.utc)
            tps = s.take_profits
            record_signal(
                symbol=s.symbol, market=self._market, timeframe=self._timeframe, tier="confirmed",
                direction=s.direction, entry_time=entry_time, entry_price=s.close, stop_loss=s.stop_loss,
                target1=tps[0], target2=tps[1] if len(tps) > 1 else None, target3=tps[2] if len(tps) > 2 else None,
                leverage=self._paper_leverage, notional_usd=self._paper_notional, score=s.score, reason=s.reason,
            )

        if self._testnet_enabled and self._market == "crypto" and results:
            self._run_testnet_execute(results)

        if self._propr_enabled and self._market == "crypto" and results:
            self._run_propr_execute(results)

    @work(thread=True, exclusive=True, group="propr")
    def _run_propr_execute(self, results) -> None:
        self._do_propr_execute(results)

    def _do_propr_execute(self, results) -> None:
        """The actual dry-run execution work, shared by both callers that
        need it: a fresh Confirmed scan (_log_paper_trades) and the Propr
        tab's own 15/30/45-min checkpoint re-checking whatever Confirmed
        last had cached -- so toggling Propr on doesn't mean waiting up to
        an hour for Confirmed's own next full scan before anything logs.
        Always called from a worker thread already; never call directly
        from the main thread."""
        if not results:
            return
        try:
            outcomes = execute_propr_signals(
                results, self._timeframe, leverage=self._paper_leverage, margin_usd=self._paper_notional,
            )
        except Exception as exc:
            self.call_from_thread(self.notify, f"Propr dry-run error: {exc}", severity="error", timeout=6)
            return
        if not outcomes:
            return
        logged = [o for o in outcomes if o.status == "dry_run"]
        skipped = [o for o in outcomes if o.status == "skipped_untradable"]
        if logged:
            symbols = ", ".join(f"{o.symbol} {o.direction}" for o in logged)
            self.call_from_thread(self.notify, f"Propr dry-run: logged {len(logged)} order(s) -- {symbols}", timeout=6)
        if skipped:
            self.call_from_thread(
                self.notify, f"Propr: {len(skipped)} signal(s) not tradable on Hyperliquid, skipped",
                timeout=5,
            )

    @work(thread=True, exclusive=True, group="testnet")
    def _run_testnet_execute(self, results) -> None:
        try:
            outcomes = execute_signals(
                results, self._timeframe, leverage=self._paper_leverage, margin_usd=self._paper_notional,
            )
        except Exception as exc:
            self.call_from_thread(self.notify, f"Testnet execution error: {exc}", severity="error", timeout=6)
            return
        if not outcomes:
            return
        placed = [o for o in outcomes if o.status == "placed"]
        failed = [o for o in outcomes if o.status == "failed"]
        if placed:
            symbols = ", ".join(f"{o.symbol} {o.direction}" for o in placed)
            self.call_from_thread(self.notify, f"Testnet: placed {len(placed)} order set(s) -- {symbols}", timeout=6)
        if failed:
            first = failed[0]
            self.call_from_thread(
                self.notify, f"Testnet: {len(failed)} failed (e.g. {first.symbol}: {first.error})",
                severity="warning", timeout=8,
            )

    def _open_paper_tab(self, force: bool = False) -> None:
        if "tab-paper" in self._scanning:
            return
        if not force and "tab-paper" in self._loaded:
            return
        self._loaded.add("tab-paper")
        self._scanning.add("tab-paper")
        self._set_status("Checking open paper trades against fresh prices...", scanning=True)
        self._run_paper_resolve()

    @work(thread=True, exclusive=True, group="paper")
    def _run_paper_resolve(self) -> None:
        try:
            resolve_open_trades()
        except Exception as exc:
            self.call_from_thread(self._scan_failed, "tab-paper", str(exc))
            return
        trades = get_trades(limit=200)
        stats = get_stats()
        self.call_from_thread(self._fill_paper, trades, stats)

    def _fill_paper(self, trades, stats) -> None:
        self._last_results["tab-paper"] = trades
        table = self.query_one("#table-paper", DataTable)
        table.clear()
        for t in trades:
            dir_text = Text("LONG", style=_LONG_STYLE) if t.direction == "long" else Text("SHORT", style=_SHORT_STYLE)
            if t.status == "open":
                status_text = Text("OPEN", style=_DIM_STYLE)
            elif t.status == "won":
                status_text = Text("WON", style=_LONG_STYLE)
            else:
                status_text = Text("LOST", style=_SHORT_STYLE)
            level_str = f"{t.best_level}x" if t.best_level > 0 else "-"
            exit_str = f"{t.exit_price:.6g}" if t.exit_price is not None else "-"
            r_str = f"{t.r_multiple:+.2f}" if t.r_multiple is not None else "-"
            pnl_str = f"{t.pnl_usd:+.2f}" if t.pnl_usd is not None else "-"
            r_style = _DIM_STYLE
            if t.r_multiple is not None:
                r_style = _LONG_STYLE if t.r_multiple > 0 else (_SHORT_STYLE if t.r_multiple < 0 else _DIM_STYLE)
            table.add_row(
                self._symbol_cell(t.symbol), t.market, t.timeframe, dir_text, status_text, level_str,
                t.entry_time.strftime("%Y-%m-%d %H:%M"), f"{t.entry_price:.6g}", f"{t.stop_loss:.6g}",
                exit_str, Text(r_str, style=r_style), Text(pnl_str, style=r_style),
                key=str(t.id),
            )
        summary = self.query_one("#paper-summary", Static)
        if stats.total == 0:
            summary.update("Paper trading: no signals logged yet -- visit Confirmed to start tracking.")
        else:
            summary.update(
                f"Paper trading: {stats.total} total ({stats.open} open, {stats.wins}W/{stats.losses}L) -- "
                f"win rate {stats.win_rate:.0f}%, avg {stats.avg_r:+.2f}R, total {stats.total_r:+.2f}R "
                f"(${stats.total_pnl_usd:+.2f} total $, at each trade's own margin x leverage)"
            )
        self._scan_done("tab-paper", len(trades))

    def _open_testnet_tab(self, force: bool = False) -> None:
        if "tab-testnet" in self._scanning:
            return
        if not force and "tab-testnet" in self._loaded:
            return
        self._loaded.add("tab-testnet")
        self._scanning.add("tab-testnet")
        self._set_status("Checking testnet orders against the real account...", scanning=True)
        self._run_testnet_resolve()

    @work(thread=True, exclusive=True, group="testnet-resolve")
    def _run_testnet_resolve(self) -> None:
        try:
            resolve_testnet_orders()
        except Exception as exc:
            self.call_from_thread(self._scan_failed, "tab-testnet", str(exc))
            return
        self.call_from_thread(self._fill_testnet_tab)

    def _fill_testnet_tab(self) -> None:
        table = self.query_one("#table-testnet", DataTable)
        self._clear_table(table)
        orders = get_testnet_orders(limit=200)
        for o in orders:
            dir_text = Text(o["direction"].upper(), style=_LONG_STYLE if o["direction"] == "long" else _SHORT_STYLE)
            status = o["status"]
            if status == "placed":
                status_text = Text("OPEN", style=_DIM_STYLE)
            elif status == "closed_tp":
                status_text = Text("CLOSED (TP)", style=_LONG_STYLE)
            elif status == "closed_stop":
                status_text = Text("CLOSED (STOP)", style=_SHORT_STYLE)
            elif status == "closed_manual":
                status_text = Text("CLOSED (MANUAL)", style=_DIM_STYLE)
            elif status == "failed":
                status_text = Text("FAILED", style=_SHORT_STYLE)
            else:
                status_text = Text(status.upper(), style=_DIM_STYLE)
            created = datetime.fromisoformat(o["created_at"]).strftime("%Y-%m-%d %H:%M")
            exit_str = f"{o['exit_price']:.6g}" if o["exit_price"] is not None else "-"
            r_str = f"{o['r_multiple']:+.2f}" if o["r_multiple"] is not None else "-"
            pnl_str = f"{o['pnl_usd']:+.2f}" if o["pnl_usd"] is not None else "-"
            r_style = _DIM_STYLE
            if o["r_multiple"] is not None:
                r_style = _LONG_STYLE if o["r_multiple"] > 0 else (_SHORT_STYLE if o["r_multiple"] < 0 else _DIM_STYLE)
            table.add_row(
                self._symbol_cell(o["symbol"]), o["timeframe"], dir_text,
                f"{o['qty']:.6g}" if o["qty"] else "-", status_text,
                f"{o['entry_price']:.6g}" if o["entry_price"] is not None else "-",
                f"{o['stop_price']:.6g}" if o["stop_price"] is not None else "-",
                f"{o['tp_price']:.6g}" if o["tp_price"] is not None else "-",
                exit_str, Text(r_str, style=r_style), Text(pnl_str, style=r_style), created, o["error"] or "",
            )
        summary = self.query_one("#testnet-summary", Static)
        if not credentials_configured():
            summary.update(
                "Testnet auto-execution: not configured -- set BINANCE_TESTNET_API_KEY / "
                "BINANCE_TESTNET_API_SECRET and restart (see Settings)."
            )
        else:
            stats = get_testnet_stats()
            failed_n = sum(1 for o in orders if o["status"] == "failed")
            on_off = "ON" if self._testnet_enabled else "OFF"
            summary.update(
                f"Testnet auto-execution: {on_off} -- {stats.total} total ({stats.open} open, "
                f"{stats.wins}W/{stats.losses}L, {failed_n} failed) -- win rate {stats.win_rate:.0f}%, "
                f"avg {stats.avg_r:+.2f}R, total {stats.total_r:+.2f}R (${stats.total_pnl_usd:+.2f} total $)"
            )
        self._scan_done("tab-testnet", len(orders))

    def _open_propr_tab(self, force: bool = False) -> None:
        if "tab-propr" in self._scanning:
            return
        if not force and "tab-propr" in self._loaded:
            return
        self._loaded.add("tab-propr")
        self._scanning.add("tab-propr")
        self._set_status("Checking Propr dry-run log against live mark price...", scanning=True)
        self._run_propr_resolve()

    @work(thread=True, exclusive=True, group="propr-resolve")
    def _run_propr_resolve(self) -> None:
        if self._propr_enabled:
            self._do_propr_execute(self._last_results.get("tab-confirmed", []))
        try:
            resolve_propr_orders()
        except Exception as exc:
            self.call_from_thread(self._scan_failed, "tab-propr", str(exc))
            return
        self.call_from_thread(self._fill_propr_tab)

    def _fill_propr_tab(self) -> None:
        table = self.query_one("#table-propr", DataTable)
        self._clear_table(table)
        orders = get_propr_orders(limit=200)
        for o in orders:
            dir_text = Text(o["direction"].upper(), style=_LONG_STYLE if o["direction"] == "long" else _SHORT_STYLE)
            status = o["status"]
            if status == "dry_run":
                status_text = Text("OPEN (dry)", style=_DIM_STYLE)
            elif status == "dry_closed_tp":
                status_text = Text("CLOSED (TP, dry)", style=_LONG_STYLE)
            elif status == "dry_closed_stop":
                status_text = Text("CLOSED (STOP, dry)", style=_SHORT_STYLE)
            else:
                status_text = Text(status.upper(), style=_DIM_STYLE)
            created = datetime.fromisoformat(o["created_at"]).strftime("%Y-%m-%d %H:%M")
            exit_str = f"{o['exit_price']:.6g}" if o["exit_price"] is not None else "-"
            r_str = f"{o['r_multiple']:+.2f}" if o["r_multiple"] is not None else "-"
            pnl_str = f"{o['pnl_usd']:+.2f}" if o["pnl_usd"] is not None else "-"
            r_style = _DIM_STYLE
            if o["r_multiple"] is not None:
                r_style = _LONG_STYLE if o["r_multiple"] > 0 else (_SHORT_STYLE if o["r_multiple"] < 0 else _DIM_STYLE)
            table.add_row(
                self._symbol_cell(o["symbol"]), o["base"] or "-", o["timeframe"], dir_text,
                f"{o['qty']:.6g}" if o["qty"] else "-", f"{o['leverage']:.0f}x" if o["leverage"] else "-",
                status_text,
                f"{o['entry_price']:.6g}" if o["entry_price"] is not None else "-",
                f"{o['stop_price']:.6g}" if o["stop_price"] is not None else "-",
                f"{o['tp_price']:.6g}" if o["tp_price"] is not None else "-",
                exit_str, Text(r_str, style=r_style), Text(pnl_str, style=r_style), created,
            )
        summary = self.query_one("#propr-summary", Static)
        if not propr_credentials_configured():
            summary.update("Propr dry-run: not configured -- set PROPR_API_KEY and restart (see Settings).")
        else:
            stats = get_propr_stats()
            on_off = "ON" if self._propr_enabled else "OFF"
            summary.update(
                f"Propr dry-run: {on_off} -- {stats.total} logged ({stats.open} open, "
                f"{stats.wins}W/{stats.losses}L) -- win rate {stats.win_rate:.0f}%, avg {stats.avg_r:+.2f}R, "
                f"total {stats.total_r:+.2f}R (${stats.total_pnl_usd:+.2f} total $) -- nothing here was ever sent"
            )
        self._scan_done("tab-propr", len(orders))

    @work(thread=True, exclusive=True, group="regime")
    def _run_regime(self) -> None:
        try:
            signal = evaluate_market_regime()
        except Exception as exc:
            self.call_from_thread(self._fill_regime_banner, None, str(exc))
            return
        self.call_from_thread(self._fill_regime_banner, signal, None)

    def _fill_regime_banner(self, signal, error: str | None) -> None:
        banner = self.query_one("#regime-banner", Static)
        if error is not None:
            banner.update(f"Market regime: unavailable ({error})")
            banner.set_class(False, "bullish")
            banner.set_class(False, "bearish")
            return
        banner.set_class(signal.label == "bullish", "bullish")
        banner.set_class(signal.label == "bearish", "bearish")
        banner.update(
            f"Market regime (crypto, 1h): {signal.label.upper()}  ({signal.score:+.0f}, {signal.trend})   "
            f"breadth {signal.bullish_count}/{signal.total} bullish vs {signal.bearish_count}/{signal.total} bearish   "
            f"BTC {signal.btc_change_pct:+.1f}%/8h   [{self._clock()}]"
        )

    def _clear_table(self, table: DataTable) -> None:
        """DataTable.clear() always resets cursor_coordinate to (0, 0) --
        if the cursor was anywhere else, that's a genuine coordinate change,
        which fires a CellHighlighted event exactly as if a human had
        clicked there. Stamping the refill time here lets the cell-copy
        handler recognize and ignore that one synthetic event instead of
        copying a cell's new content to the clipboard on every scan."""
        table.clear()
        self._table_refilled_at[table.id] = time.time()

    def _clear_tables(self, *tables: DataTable) -> None:
        for table in tables:
            self._clear_table(table)

    def _scan_done(self, tab_id: str, count: int) -> None:
        self._scanning.discard(tab_id)
        self._set_status(f"{tab_id.replace('tab-', '').title()}: {count} result(s) -- {self._clock()}")
        if self._auto_refresh and tab_id not in self._auto_timers and tab_id in self._auto_refresh_tabs:
            self._start_auto_timer_for(tab_id)  # newly-loaded tab joins the rotation
            self._update_auto_label()

    def _scan_failed(self, tab_id: str, error: str) -> None:
        self._loaded.discard(tab_id)
        self._scanning.discard(tab_id)
        self._set_status(f"{tab_id.replace('tab-', '').title()} scan failed: {error}")

    @staticmethod
    def _clock() -> str:
        import time
        return time.strftime("%H:%M:%S")


def run() -> None:
    ScannerApp().run()


if __name__ == "__main__":
    run()
