"""A live dashboard for the scanner, built on Textual.

Pure presentation layer: every number on screen comes from the exact same
scan()/scan_approaching()/scan_pullback()/scan_accumulation()/scan_gold_smc()
functions the CLI (scan.py) uses. Nothing here recomputes a signal or
duplicates detection logic -- this only decides how to lay it out, color it,
and keep it refreshed.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

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
    GOLD_SYMBOL,
    evaluate_market_regime,
    fetch_crypto_news,
    fetch_stock_news,
    get_stats,
    get_trades,
    record_signal,
    resolve_open_trades,
    run_backtest,
    scan,
    scan_accumulation,
    scan_approaching,
    scan_gold_smc,
    scan_pullback,
    timeframe_to_seconds,
)

from .favorites_store import load_favorites, save_favorites

CSS_FILE = Path(__file__).parent / "theme.tcss"

TIMEFRAMES = [("15m", "15m"), ("1h", "1h"), ("4h", "4h"), ("1d", "1d")]
MARKETS = [("Crypto", "crypto"), ("Stocks (perp)", "stocks"), ("Real Stocks", "stocks-real")]

# yfinance (Real Stocks) has no native 4h bar -- see stock_data.py. Coerced
# to 1d automatically rather than erroring on every single symbol.
_NO_4H_MARKETS = {"stocks-real"}

_LONG_STYLE = "bold #3fb950"
_SHORT_STYLE = "bold #f85149"
_ENTRY_STYLE = "bold #58a6ff"
_STOP_STYLE = "bold #f85149"
_TARGET_STYLE = "bold #3fb950"
_DIM_STYLE = "#6e7681"


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


class SettingsScreen(ModalScreen):
    """App-wide settings that affect future behavior, not current data --
    currently just paper-trading sizing. A home for anything that would
    otherwise clutter a tab's own control bar with cross-cutting config.

    Changes here take effect immediately for whatever happens NEXT (the next
    signal logged), but never retroactively touch trades already in the
    ledger -- each trade locks in whatever was set at the moment it was
    recorded (see paper_trading.record_signal's docstring)."""

    BINDINGS = [("escape", "dismiss", "Close"), ("s", "dismiss", "Close")]

    def __init__(self, leverage: float, notional: float) -> None:
        super().__init__()
        self._leverage = leverage
        self._notional = notional

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
            yield Label("Press Esc or s to close", id="settings-close-hint")

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
        ("s", "show_settings", "Settings"),
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
        self._favorites: dict[str, dict] = load_favorites()
        self._news_category = "crypto"
        self._paper_leverage = 1.0
        self._paper_notional = 10.0

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
            with TabPane("Gold SMC", id="tab-gold"):
                with Horizontal(id="gold-controls"):
                    yield Label("HTF")
                    yield Select([("4h", "4h"), ("1d", "1d")], value="4h", id="gold-htf", allow_blank=False)
                    yield Label("LTF")
                    yield Select([("15m", "15m"), ("1h", "1h")], value="15m", id="gold-ltf", allow_blank=False)
                    yield Button("Scan Now", id="gold-scan-btn")
                yield Static("Press Scan Now to check XAU/USDT.", id="gold-card")
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
                    "for the full reason and detail.",
                    classes="panel-subtitle",
                )
                yield DataTable(id="table-paper", zebra_stripes=True, cursor_type="row")
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
        return {
            "table-confirmed-long": confirmed_cols,
            "table-confirmed-short": confirmed_cols,
            "table-approaching-long": approaching_cols,
            "table-approaching-short": approaching_cols,
            "table-pullback-long": ["Symbol", "Score", "Retrace%", "Vol x", "Entry", "Stop", "T1", "T2", "Reason"],
            "table-pullback-short": ["Symbol", "Score", "Retrace%", "Vol x", "Entry", "Stop", "T1", "T2", "Reason"],
            "table-accumulation": ["Symbol", "Score", "Range%", "ATR%", "Pos", "Reason"],
            "table-backtest": ["Dir", "Entry Time", "Entry", "Exit Time", "Exit", "Reason", "R"],
            "table-news": ["Time", "Impact", "Source", "Headline"],
            "table-paper": [
                "Symbol", "Market", "TF", "Dir", "Status", "TPs Hit", "Entry Time", "Entry",
                "Stop", "Exit", "R", "$PnL",
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

        elif event.select.id == "settings-notional-select":
            new_notional = float(event.value)
            if new_notional == self._paper_notional:
                return
            self._paper_notional = new_notional

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "scan-now-btn":
            self._scan_tab(self._active_tab_id(), force=True)
        elif event.button.id == "gold-scan-btn":
            self._scan_gold(force=True)
        elif event.button.id == "backtest-run-btn":
            self._start_backtest()
        elif event.button.id == "news-refresh-btn":
            self._open_news_tab(force=True)

    def _refresh_active_tab(self) -> None:
        """Same dispatch on_tabbed_content_tab_activated uses, for when the
        Market/Timeframe controls (not a tab click) are what just went stale."""
        active = self._active_tab_id()
        if active == "tab-gold":
            self._scan_gold(force=True)
        elif active not in ("tab-favorites", "tab-backtest", "tab-news", "tab-paper"):
            self._scan_tab(active)

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "auto-switch":
            self._auto_refresh = event.value
            self._restart_auto_timer()

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
            "tab-confirmed", "tab-approaching", "tab-pullback", "tab-accumulation",
            "tab-gold", "tab-backtest", "tab-news", "tab-paper", "tab-favorites",
        )
        for known in known_tabs:
            if known in tab_id:
                if known == "tab-gold":
                    if "tab-gold" not in self._loaded:
                        self._scan_gold()
                elif known == "tab-news":
                    if "tab-news" not in self._loaded:
                        self._open_news_tab()
                elif known == "tab-paper":
                    if "tab-paper" not in self._loaded:
                        self._open_paper_tab()
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
        active = self._active_tab_id()
        if active == "tab-gold":
            self._scan_gold(force=True)
        else:
            self._scan_tab(active, force=True)

    def action_show_glossary(self) -> None:
        self.push_screen(GlossaryScreen())

    def action_show_settings(self) -> None:
        self.push_screen(SettingsScreen(leverage=self._paper_leverage, notional=self._paper_notional))

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
        table.clear()
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
        }
        for tab_id, filler in fillers.items():
            if tab_id in self._last_results:
                filler(self._last_results[tab_id])

    # ---- auto-refresh: every LOADED tab refreshes independently, aligned to
    # its own candle close -- not just whichever tab happens to be on screen. ----

    def _timeframe_for(self, tab_id: str) -> str:
        if tab_id == "tab-gold":
            return self.query_one("#gold-ltf", Select).value
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
            self._start_auto_timer_for(tab_id)
        self._update_auto_label()

    def _start_auto_timer_for(self, tab_id: str) -> None:
        try:
            seconds = timeframe_to_seconds(self._timeframe_for(tab_id))
        except ValueError:
            seconds = 3600
        now = time.time()
        next_close = (now // seconds + 1) * seconds
        delay = max(5.0, next_close - now + 3)
        self._auto_timers[tab_id] = self.set_timer(delay, lambda t=tab_id: self._auto_refresh_fire(t))

    def _auto_refresh_fire(self, tab_id: str) -> None:
        if tab_id == "tab-gold":
            self._scan_gold(force=True)
        elif tab_id == "tab-news":
            self._open_news_tab(force=True)
        elif tab_id == "tab-paper":
            self._open_paper_tab(force=True)
        else:
            self._scan_tab(tab_id, force=True)
        if self._auto_refresh:
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
        long_table.clear()
        short_table.clear()
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
        long_table.clear()
        short_table.clear()
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
        long_table.clear()
        short_table.clear()
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
        table.clear()
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
        table.clear()
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

    # ---- news: lazy-loaded like Gold SMC, category switch forces a refetch ----

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

    def _scan_gold(self, force: bool = False) -> None:
        if "tab-gold" in self._scanning:
            return
        if not force and "tab-gold" in self._loaded:
            return
        self._loaded.add("tab-gold")
        self._scanning.add("tab-gold")
        self._set_status("Scanning XAU/USDT...", scanning=True)
        htf = self.query_one("#gold-htf", Select).value
        ltf = self.query_one("#gold-ltf", Select).value
        self._run_gold(htf, ltf)

    @work(thread=True)
    def _run_gold(self, htf: str, ltf: str) -> None:
        try:
            results = scan_gold_smc(htf_timeframe=htf, ltf_timeframe=ltf)
        except Exception as exc:
            self.call_from_thread(self._scan_failed, "tab-gold", str(exc))
            return
        self.call_from_thread(self._fill_gold, results)

    def _fill_gold(self, results) -> None:
        card = self.query_one("#gold-card", Static)
        if not results:
            card.update(f"No qualifying SMC sweep setup on {GOLD_SYMBOL} this scan.")
            card.set_class(False, "long")
            card.set_class(False, "short")
        else:
            s = results[0]
            card.set_class(s.direction == "long", "long")
            card.set_class(s.direction == "short", "short")
            align = "aligned with" if s.aligned_with_bias else "COUNTER to"
            targets = "   ".join(f"T{i + 1} [{_TARGET_STYLE}]{t:.2f}[/]" for i, t in enumerate(s.targets))
            direction_markup = (
                f"[{_LONG_STYLE}]LONG[/]" if s.direction == "long" else f"[{_SHORT_STYLE}]SHORT[/]"
            )
            card.update(
                f"{GOLD_SYMBOL}  {direction_markup}   (HTF bias: {s.htf_bias}, {align} bias, in {s.premium_discount})\n\n"
                f"Zone: {s.zone_kind} at ${s.zone_level:.2f}   Volume {s.volume_ratio:.1f}x avg\n\n"
                f"[{_ENTRY_STYLE}]Entry {s.entry:.2f}[/]   [{_STOP_STYLE}]Stop {s.stop_loss:.2f}[/]   {targets}\n\n"
                f"{s.reason}"
            )
        self._scan_done("tab-gold", len(results))

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

    def _scan_done(self, tab_id: str, count: int) -> None:
        self._scanning.discard(tab_id)
        self._set_status(f"{tab_id.replace('tab-', '').title()}: {count} result(s) -- {self._clock()}")
        if self._auto_refresh and tab_id not in self._auto_timers:
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
