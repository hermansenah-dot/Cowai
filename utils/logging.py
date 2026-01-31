"""Logging utilities for the Discord bot."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytz

if TYPE_CHECKING:
    from pytz.tzinfo import BaseTzInfo

# Default timezone for timestamps
DEFAULT_TZ: BaseTzInfo = pytz.timezone("Europe/Copenhagen")


def log(message: str, tz: BaseTzInfo | None = None) -> None:
    """Print console messages with a local timestamp."""
    tz = tz or DEFAULT_TZ
    ts = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {message}")


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
