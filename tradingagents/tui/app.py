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
            self._scan_tab(self._active_tab_id())
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
            self._scan_tab(self._active_tab_id())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "scan-now-btn":
            self._scan_tab(self._active_tab_id(), force=True)
        elif event.button.id == "gold-scan-btn":
            self._scan_gold(force=True)

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "auto-switch":
            self._auto_refresh = event.value
            self._restart_auto_timer()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        tab_id = event.tab.id or ""
        # Tabs/TabPane ids differ by a "--content-tab-" prefix internally; match by suffix.
        known_tabs = ("tab-confirmed", "tab-approaching", "tab-pullback", "tab-accumulation", "tab-gold", "tab-favorites")
        for known in known_tabs:
            if known in tab_id:
                if known == "tab-gold":
                    if "tab-gold" not in self._loaded:
                        self._scan_gold()
                elif known == "tab-favorites":
                    pass  # static local data, nothing to fetch
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

    @work(thread=True, exclusive=True)
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
