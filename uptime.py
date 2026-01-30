# uptime.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
import time
from typing import Optional


@dataclass
class UptimeTracker:
    tz: tzinfo

    started_at: datetime
    started_mono: float

    connects: int = 0
    reconnects: int = 0
    disconnects: int = 0
    resumes: int = 0

    _ever_connected: bool = False

    @classmethod
    def start(cls, tz: tzinfo) -> "UptimeTracker":
        return cls(
            tz=tz,
            started_at=datetime.now(tz),
            started_mono=time.monotonic(),
        )

    def mark_connect(self) -> None:
        self.connects += 1
        if self._ever_connected:
            self.reconnects += 1
        self._ever_connected = True

    def mark_disconnect(self) -> None:
        self.disconnects += 1

    def mark_resume(self) -> None:
        self.resumes += 1

    def uptime(self) -> timedelta:
        return timedelta(seconds=int(time.monotonic() - self.started_mono))

    def format_status(self) -> str:
        up = self.uptime()
        days = up.days
        hours, rem = divmod(up.seconds, 3600)
        mins, secs = divmod(rem, 60)

        up_str = (f"{days}d " if days else "") + f"{hours:02d}:{mins:02d}:{secs:02d}"
        since_str = self.started_at.strftime("%Y-%m-%d %H:%M:%S %Z")

        return (
            f"⏱️ Uptime: **{up_str}** (since {since_str})\n"
            f"🔌 Reconnects: **{self.reconnects}**\n"
            f"❌ Disconnects: **{self.disconnects}**\n"
            f"♻️ Session resumes: **{self.resumes}**"
        )


# Bot will set this on startup.
TRACKER: Optional[UptimeTracker] = None
