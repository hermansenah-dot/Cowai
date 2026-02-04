"""Logging utilities for the Discord bot."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytz

if TYPE_CHECKING:
    from pytz.tzinfo import BaseTzInfo

# Enable ANSI colors on Windows
if sys.platform == "win32":
    os.system("")  # Enables ANSI escape sequences in Windows terminal

# ANSI color codes
class Colors:
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    RESET = "\033[0m"

# Default timezone for timestamps
DEFAULT_TZ: BaseTzInfo = pytz.timezone("Europe/Copenhagen")


def log(message: str, tz: BaseTzInfo | None = None) -> None:
    """Print console messages with a local timestamp."""
    tz = tz or DEFAULT_TZ
    ts = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {message}")


def log_user(message: str, tz: BaseTzInfo | None = None) -> None:
    """Print user input in RED with a local timestamp."""
    tz = tz or DEFAULT_TZ
    ts = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{Colors.RED}[{ts}] {message}{Colors.RESET}")


def log_ai(message: str, tz: BaseTzInfo | None = None) -> None:
    """Print AI response in GREEN with a local timestamp."""
    tz = tz or DEFAULT_TZ
    ts = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{Colors.GREEN}[{ts}] {message}{Colors.RESET}")


def log_to_file(
    filepath: str | Path,
    message: str,
    *,
    tz: BaseTzInfo | None = None,
    create_parents: bool = True,
) -> None:
    """Append a timestamped message to a file."""
    tz = tz or DEFAULT_TZ
    ts = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    
    path = Path(filepath)
    if create_parents:
        path.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception as e:
        # Logging should never break the bot
        try:
            print(f"[{ts}] log write failed: {e} | path={filepath}")
        except Exception:
            pass
