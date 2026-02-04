"""Common utility helpers used across the project."""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING

import pytz

if TYPE_CHECKING:
    from pytz.tzinfo import BaseTzInfo

__all__ = ["clamp", "now_ts", "get_current_time"]


def clamp(value: float, lo: float, hi: float) -> float:
    """Clamp a value between lo and hi (inclusive)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = 0.0
    return max(lo, min(hi, v))


def now_ts() -> int:
    """Return current Unix timestamp as integer."""
    return int(time.time())


def get_current_time(tz_name: str = "Europe/Copenhagen") -> str:
    """Get current time as HH:MM string in the specified timezone."""
    tz: BaseTzInfo = pytz.timezone(tz_name)
    now = datetime.now(tz)
    return now.strftime("%H:%M")
