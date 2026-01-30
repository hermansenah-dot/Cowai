"""trust.py

Simple, persistent per-user trust system.

Goals:
- Provide a numeric trust score in [0.0, 1.0] per Discord user.
- Keep a small event log for explainability/debugging.
- Support using trust to influence:
  - how strongly user messages affect the mood engine
  - how "relaxed" / informal the assistant may be in tone

This module is intentionally lightweight (sqlite3 + stdlib only).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple


def _now_ts() -> int:
    return int(time.time())


def _clampf(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


@dataclass(frozen=True)
class TrustStyle:
    score: float
    relax: float
    mood_multiplier: float


class TrustStore:
    """SQLite-backed trust scores + event log."""

    def __init__(self, db_path: str = "memory/trust.db"):
        self.db_path = str(db_path)
        parent = Path(self.db_path).parent
        if str(parent) != ".":
            parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS trust (
                    user_id TEXT PRIMARY KEY,
                    score REAL NOT NULL,
                    updated_ts INTEGER NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS trust_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    delta REAL NOT NULL,
                    reason TEXT NOT NULL
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_trust_events_user_ts ON trust_events(user_id, ts DESC)"
            )
            self.conn.commit()

    def get_score(self, user_id: int, default: float = 0.50) -> float:
        uid = str(int(user_id))
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("SELECT score FROM trust WHERE user_id=?", (uid,))
            row = cur.fetchone()

        if not row:
            return float(_clampf(default, 0.0, 1.0))

        try:
            return float(_clampf(row["score"], 0.0, 1.0))
        except Exception:
            return float(_clampf(default, 0.0, 1.0))

    def set_score(self, user_id: int, score: float, *, reason: str = "manual set") -> float:
        uid = str(int(user_id))
        score = float(_clampf(score, 0.0, 1.0))
        now = _now_ts()

        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                """
                INSERT INTO trust(user_id, score, updated_ts)
                VALUES(?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                  score=excluded.score,
                  updated_ts=excluded.updated_ts
                """,
                (uid, score, now),
            )
            cur.execute(
                "INSERT INTO trust_events(user_id, ts, delta, reason) VALUES(?, ?, ?, ?)",
                (uid, now, 0.0, str(reason)[:500]),
            )
            self.conn.commit()

        return score

    def add(self, user_id: int, delta: float, *, reason: str) -> float:
        delta = float(delta)
        current = self.get_score(user_id)
        new_score = float(_clampf(current + delta, 0.0, 1.0))
        uid = str(int(user_id))
        now = _now_ts()

        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                """
                INSERT INTO trust(user_id, score, updated_ts)
                VALUES(?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                  score=excluded.score,
                  updated_ts=excluded.updated_ts
                """,
                (uid, new_score, now),
            )
            cur.execute(
                "INSERT INTO trust_events(user_id, ts, delta, reason) VALUES(?, ?, ?, ?)",
                (uid, now, float(delta), str(reason)[:500]),
            )
            self.conn.commit()

        return new_score

    def recent_events(self, user_id: int, limit: int = 6) -> List[Tuple[int, float, str]]:
        uid = str(int(user_id))
        limit = max(1, min(int(limit), 20))

        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT ts, delta, reason FROM trust_events WHERE user_id=? ORDER BY ts DESC LIMIT ?",
                (uid, limit),
            )
            rows = cur.fetchall()

        out: List[Tuple[int, float, str]] = []
        for r in rows:
            try:
                out.append((int(r["ts"]), float(r["delta"]), str(r["reason"])))
            except Exception:
                continue
        return out

    def style(self, user_id: int) -> TrustStyle:
        """Return style parameters derived from trust score.

        relax: [0..1] — how casual/expressive the assistant can be.
        mood_multiplier: [0.6..1.8] — scales how much messages affect the mood engine.
        """
        score = self.get_score(user_id)
        relax = float(_clampf((score - 0.20) / 0.80, 0.0, 1.0))
        mood_multiplier = float(_clampf(1.0 + (score - 0.50) * 1.2, 0.6, 1.8))
        return TrustStyle(score=score, relax=relax, mood_multiplier=mood_multiplier)

    def prompt_block(self, user_id: int) -> str:
        s = self.style(user_id)
        if s.relax >= 0.70:
            vibe = "high"
        elif s.relax >= 0.35:
            vibe = "medium"
        else:
            vibe = "low"

        return (
            "User trust context (internal):\n"
            f"- Trust score: {s.score:.2f} / 1.00\n"
            f"- Relax level: {vibe} (be more casual/expressive as trust increases)\n"
            "Guidance:\n"
            "- Higher trust: be more relaxed, playful, and emotionally expressive; you may use light humor and be less formal.\n"
            "- Lower trust: keep a more neutral, professional tone; avoid overly personal assumptions.\n"
            "- Always follow safety rules and avoid disallowed content."
        ).strip()


trust = TrustStore()
