"""Entry point for the standalone scanner TUI executable (see build_tui_exe.* for
how this gets packaged with PyInstaller). Running this file directly with
`python scanner_tui.py` also works during development."""

from tradingagents.tui.app import run

if __name__ == "__main__":
    run()
