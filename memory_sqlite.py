"""memory_sqlite.py

SQLite-backed long-term memory for Discord users.

Stores:
- facts: stable key/value preferences (name, language, project, etc.)
- episodes: short "memory cards" capturing conversation details with tags + importance
- messages: raw recent chat logs used for optional LLM-based extraction

This module is conservative:
- it injects only the most relevant items into prompts
- extraction runs only every N messages to keep replies fast

Requires: Python standard library only (sqlite3).
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _now_ts() -> int:
    return int(time.time())


def _norm_words(text: str) -> List[str]:
    words = re.findall(r"[a-zA-Z0-9']{2,}", text.lower())
    seen = set()
    out: List[str] = []
    for w in words:
        if w not in seen:
            out.append(w)
            seen.add(w)
    return out


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


@dataclass
class Episode:
    id: int
    user_id: str
    ts: int
    text: str
    tags: str
    importance: float


class SQLiteMemory:
    """
    One SQLite DB for all users.

    Note:
    - discord.py typically runs in one event loop thread, so one connection is fine.
    - if you later run extraction in a thread executor, make a separate connection per thread.
    """

    def __init__(self, db_path: str = "memory.db"):
        self.db_path = str(db_path)
        parent = Path(self.db_path).parent
        if str(parent) != ".":
            parent.mkdir(parents=True, exist_ok=True)

        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

        # Per-user message counters for "extract every N messages"
        self._msg_counter: Dict[str, int] = {}

    # -------------------------
    # Schema
    # -------------------------

    def _init_schema(self) -> None:
        cur = self.conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS facts (
                user_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.7,
                updated_ts INTEGER NOT NULL,
                PRIMARY KEY (user_id, key)
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                ts INTEGER NOT NULL,
                text TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT "",
                importance REAL NOT NULL DEFAULT 0.5
            )
            """
        )

        cur.execute("CREATE INDEX IF NOT EXISTS idx_episodes_user_ts ON episodes(user_id, ts DESC)")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                ts INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL
            )
            """
        )

        cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_user_ts ON messages(user_id, ts DESC)")

        self.conn.commit()

    # -------------------------
    # Facts
    # -------------------------

    def upsert_fact(self, user_id: int, key: str, value: str, confidence: float = 0.7) -> None:
        uid = str(user_id)
        key = key.strip().lower()
        value = value.strip()
        confidence = float(_clamp(confidence, 0.0, 1.0))

        if not key or not value:
            return

        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO facts(user_id, key, value, confidence, updated_ts)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(user_id, key) DO UPDATE SET
              value=excluded.value,
              confidence=excluded.confidence,
              updated_ts=excluded.updated_ts
            """,
            (uid, key, value, confidence, _now_ts()),
        )
        self.conn.commit()

    def get_facts(self, user_id: int) -> Dict[str, str]:
        uid = str(user_id)
        cur = self.conn.cursor()
        cur.execute("SELECT key, value FROM facts WHERE user_id=? ORDER BY updated_ts DESC", (uid,))
        return {row["key"]: row["value"] for row in cur.fetchall()}

    def facts_as_prompt(self, user_id: int) -> str:
        facts = self.get_facts(user_id)
        if not facts:
            return ""

        lines: List[str] = []
        if facts.get("name"):
            lines.append(f"The user's name is {facts['name']}.")
        if facts.get("preferred_language"):
            lines.append(f"Preferred language: {facts['preferred_language']}.")

        # Keep likes/dislikes concise if present
        if facts.get("likes"):
            lines.append(f"User likes: {facts['likes']}.")
        if facts.get("dislikes"):
            lines.append(f"User dislikes: {facts['dislikes']}.")

        # Extra keys (kept short)
        for k, v in facts.items():
            if k in {"name", "preferred_language", "likes", "dislikes"}:
                continue
            if v and len(v) <= 200:
                lines.append(f"{k}: {v}")

        return "\n".join(lines).strip()

    # -------------------------
    # Episodes (memory cards)
    # -------------------------

    def add_episode(
        self,
        user_id: int,
        text: str,
        tags: Iterable[str] = (),
        importance: float = 0.5,
        ts: Optional[int] = None,
    ) -> None:
        uid = str(user_id)
        text = (text or "").strip()
        if not text:
            return

        tags_str = ", ".join([str(t).strip().lower() for t in tags if str(t).strip()])
        importance = float(_clamp(importance, 0.0, 1.0))
        ts = int(ts or _now_ts())

        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO episodes(user_id, ts, text, tags, importance) VALUES(?, ?, ?, ?, ?)",
            (uid, ts, text, tags_str, importance),
        )
        self.conn.commit()

    def _fetch_candidate_episodes(self, user_id: int, limit: int = 120) -> List[Episode]:
        uid = str(user_id)
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, user_id, ts, text, tags, importance FROM episodes WHERE user_id=? ORDER BY ts DESC LIMIT ?",
            (uid, int(limit)),
        )
        rows = cur.fetchall()
        return [
            Episode(
                id=int(row["id"]),
                user_id=str(row["user_id"]),
                ts=int(row["ts"]),
                text=str(row["text"]),
                tags=str(row["tags"]),
                importance=float(row["importance"]),
            )
            for row in rows
        ]

    def retrieve_relevant(self, user_id: int, query: str, limit: int = 6) -> List[Episode]:
        query = (query or "").strip()
        if not query:
            return []

        qwords = set(_norm_words(query))
        if not qwords:
            return []

        candidates = self._fetch_candidate_episodes(user_id, limit=160)
        now = _now_ts()

        scored: List[Tuple[float, Episode]] = []
        for ep in candidates:
            blob = f"{ep.text} {ep.tags}".lower()
            ewords = set(_norm_words(blob))
            overlap = len(qwords.intersection(ewords))
            if overlap <= 0:
                continue

            age_days = max(0.0, (now - ep.ts) / 86400.0)
            recency = _clamp(1.0 - (age_days / 30.0), 0.0, 1.0)

            score = (overlap * 2.0) + (ep.importance * 1.5) + (recency * 1.0)
            scored.append((score, ep))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [ep for _, ep in scored[: int(limit)]]

    def episodes_as_prompt(self, episodes: List[Episode]) -> str:
        if not episodes:
            return ""
        lines: List[str] = []
        for ep in episodes:
            dt = time.strftime("%Y-%m-%d", time.localtime(ep.ts))
            tag_part = f" [{ep.tags}]" if ep.tags else ""
            lines.append(f"- ({dt}) {ep.text}{tag_part}")
        return "\n".join(lines)

    # -------------------------
    # Message log (optional)
    # -------------------------

    def add_message(self, user_id: int, role: str, content: str, ts: Optional[int] = None) -> None:
        uid = str(user_id)
        role = (role or "").strip().lower()
        content = (content or "").strip()
        if role not in {"user", "assistant", "system"}:
            role = "user"
        if not content:
            return

        ts = int(ts or _now_ts())
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO messages(user_id, ts, role, content) VALUES(?, ?, ?, ?)",
            (uid, ts, role, content),
        )
        self.conn.commit()

        self._msg_counter[uid] = self._msg_counter.get(uid, 0) + 1

    def get_recent_messages(self, user_id: int, limit: int = 12) -> List[Dict[str, str]]:
        uid = str(user_id)
        cur = self.conn.cursor()
        cur.execute(
            "SELECT role, content FROM messages WHERE user_id=? ORDER BY ts DESC LIMIT ?",
            (uid, int(limit)),
        )
        rows = cur.fetchall()
        return [{"role": str(r["role"]), "content": str(r["content"])} for r in rows][::-1]

    # -------------------------
    # LLM extraction (optional)
    # -------------------------

    def should_extract(self, user_id: int, every_n_messages: int = 8) -> bool:
        uid = str(user_id)
        return self._msg_counter.get(uid, 0) >= every_n_messages

    def reset_extract_counter(self, user_id: int) -> None:
        self._msg_counter[str(user_id)] = 0

    def extract_and_store(self, user_id: int, ask_llama_fn, window: int = 12) -> None:
        messages = self.get_recent_messages(user_id, limit=window)
        if not messages:
            return

        sys = {
            "role": "system",
            "content": (
                "You are a STRICT memory extraction tool. Output JSON ONLY.\n"
                "Extract stable facts and a few key episodic memories.\n\n"
                "Schema:\n"
                "{\n"
                '  "facts": [{"key": str, "value": str, "confidence": 0..1}],\n'
                '  "episodes": [{"text": str, "tags": [str], "importance": 0..1}]\n'
                "}\n\n"
                "Rules:\n"
                "- facts are stable preferences/identity/projects. No one-off chatter.\n"
                "- episodes: 0-3 max, each <= 280 chars.\n"
                "- If nothing is worth saving: return empty lists.\n"
            ),
        }

        raw = ask_llama_fn([sys] + messages)

        try:
            data = json.loads(raw)
        except Exception:
            return

        if not isinstance(data, dict):
            return

        facts = data.get("facts", [])
        if isinstance(facts, list):
            for f in facts[:10]:
                if not isinstance(f, dict):
                    continue
                key = str(f.get("key", "")).strip()
                val = str(f.get("value", "")).strip()
                try:
                    conf = float(f.get("confidence", 0.7))
                except Exception:
                    conf = 0.7
                if not key or not val:
                    continue
                if len(key) > 48 or len(val) > 240:
                    continue
                self.upsert_fact(user_id, key, val, confidence=conf)

        eps = data.get("episodes", [])
        if isinstance(eps, list):
            for e in eps[:3]:
                if not isinstance(e, dict):
                    continue
                text = str(e.get("text", "")).strip()
                if not text or len(text) > 280:
                    continue

                tags = e.get("tags", [])
                if not isinstance(tags, list):
                    tags = []
                tags = [str(t)[:24] for t in tags if str(t).strip()][:8]

                try:
                    imp = float(e.get("importance", 0.5))
                except Exception:
                    imp = 0.5

                self.add_episode(user_id, text=text, tags=tags, importance=imp)

    # -------------------------
    # Prompt injection
    # -------------------------

    def build_prompt_injection(self, user_id: int, user_query: str, max_episodes: int = 6) -> str:
        episodes = self.retrieve_relevant(user_id, user_query, limit=max_episodes)
        ep_block = self.episodes_as_prompt(episodes)
        facts_block = self.facts_as_prompt(user_id)

        parts: List[str] = []
        if ep_block:
            parts.append("Relevant memories:\n" + ep_block)
        if facts_block:
            parts.append("Known facts:\n" + facts_block)

        return "\n\n".join(parts).strip()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
