"""Terminal color helpers for scanner output.

colorama.init() patches stdout on Windows so ANSI codes render in cmd.exe /
older terminals; it's a no-op on platforms that already support ANSI. Colors
degrade to plain text automatically when output isn't a real terminal (piped
to a file, redirected) via colorama's strip-on-non-tty behavior.
"""

from __future__ import annotations

from colorama import Fore, Style, init

init(autoreset=True)


def entry(text: str) -> str:
    return f"{Fore.CYAN}{text}{Style.RESET_ALL}"


def stop(text: str) -> str:
    return f"{Fore.RED}{text}{Style.RESET_ALL}"


def target(text: str) -> str:
    return f"{Fore.GREEN}{text}{Style.RESET_ALL}"


def long_label(text: str) -> str:
    return f"{Fore.GREEN}{Style.BRIGHT}{text}{Style.RESET_ALL}"


def short_label(text: str) -> str:
    return f"{Fore.RED}{Style.BRIGHT}{text}{Style.RESET_ALL}"


def dim(text: str) -> str:
    return f"{Style.DIM}{text}{Style.RESET_ALL}"
