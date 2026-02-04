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
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from utils.helpers import clamp, now_ts

# Vector embeddings (lazy import to avoid startup cost if Ollama is down)
_vector_module = None

def _get_vector_module():
    """Lazy-load vector module to avoid import errors if numpy missing."""
    global _vector_module
    if _vector_module is None:
        try:
            import memory_vector as mv
            _vector_module = mv
        except ImportError:
            _vector_module = False  # Mark as unavailable
    return _vector_module if _vector_module else None


def _norm_words(text: str) -> List[str]:
    words = re.findall(r"[a-zA-Z0-9']{2,}", text.lower())
    seen = set()
    out: List[str] = []
    for w in words:
        if w not in seen:
            out.append(w)
            seen.add(w)
    return out


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

        # allow use from asyncio thread executors; serialize writes with a lock
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
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
                importance REAL NOT NULL DEFAULT 0.5,
                times_used INTEGER NOT NULL DEFAULT 0,
                last_used_ts INTEGER NOT NULL DEFAULT 0
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

        # Lightweight schema migration for older DBs (ALTER TABLE is additive).
        cur.execute("PRAGMA table_info(episodes)")
        existing = {str(r["name"]) for r in cur.fetchall()}
        if "times_used" not in existing:
            cur.execute("ALTER TABLE episodes ADD COLUMN times_used INTEGER NOT NULL DEFAULT 0")
        if "last_used_ts" not in existing:
            cur.execute("ALTER TABLE episodes ADD COLUMN last_used_ts INTEGER NOT NULL DEFAULT 0")
        if "embedding" not in existing:
            cur.execute("ALTER TABLE episodes ADD COLUMN embedding BLOB DEFAULT NULL")

        self.conn.commit()

    # -------------------------
    # Housekeeping
    # -------------------------

    def prune(self, user_id: int, *, keep_episodes: int = 600, keep_messages: int = 300) -> None:
        """Keep DB size bounded per user."""
        uid = str(user_id)
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                """
                DELETE FROM episodes
                WHERE user_id=?
                  AND id NOT IN (
                    SELECT id FROM episodes WHERE user_id=? ORDER BY ts DESC LIMIT ?
                  )
                """,
                (uid, uid, int(keep_episodes)),
            )
            cur.execute(
                """
                DELETE FROM messages
                WHERE user_id=?
                  AND id NOT IN (
                    SELECT id FROM messages WHERE user_id=? ORDER BY ts DESC LIMIT ?
                  )
                """,
                (uid, uid, int(keep_messages)),
            )
            self.conn.commit()

    @staticmethod
    def redact(text: str) -> str:
        """Best-effort redaction to avoid storing secrets in memory."""
        t = (text or "")
        # Discord tokens (very rough heuristic)
        t = re.sub(r"[MN][A-Za-z\d_-]{20,}\.[A-Za-z\d_-]{6,}\.[A-Za-z\d_-]{20,}", "[REDACTED_TOKEN]", t)
        # Common API key shapes
        t = re.sub(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*\S+", r"\1=[REDACTED]", t)
        return t

    # -------------------------
    # Facts
    # -------------------------

    def upsert_fact(self, user_id: int, key: str, value: str, confidence: float = 0.7) -> None:
        uid = str(user_id)
        key = key.strip().lower()
        value = value.strip()
        confidence = float(clamp(confidence, 0.0, 1.0))

        if not key or not value:
            return

        with self._lock:
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
                (uid, key, value, confidence, now_ts()),
            )
            self.conn.commit()

    def get_facts(self, user_id: int) -> Dict[str, str]:
        uid = str(user_id)
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("SELECT key, value FROM facts WHERE user_id=? ORDER BY updated_ts DESC", (uid,))
            return {row["key"]: row["value"] for row in cur.fetchall()}

    def facts_as_prompt(self, user_id: int) -> str:
        facts = self.get_facts(user_id)
        if not facts:
            return ""

        lines: List[str] = []
        if facts.get("name"):
            # Be explicit to avoid confusion with bot's name
            lines.append(f"The person you're talking to is named \"{facts['name']}\" - address them by this name, not yours.")
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
        embed: bool = True,
    ) -> None:
        uid = str(user_id)
        text = (text or "").strip()
        if not text:
            return

        tags_str = ", ".join([str(t).strip().lower() for t in tags if str(t).strip()])
        importance = float(clamp(importance, 0.0, 1.0))
        ts = int(ts or now_ts())

        # Generate embedding if enabled
        embedding_blob: Optional[bytes] = None
        if embed:
            mv = _get_vector_module()
            if mv:
                embed_text = f"{text} {tags_str}".strip()
                embedding = mv.embed_text(embed_text)
                if embedding:
                    embedding_blob = mv.embedding_to_blob(embedding)

        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "INSERT INTO episodes(user_id, ts, text, tags, importance, embedding) VALUES(?, ?, ?, ?, ?, ?)",
                (uid, ts, text, tags_str, importance, embedding_blob),
            )
            self.conn.commit()

    def _fetch_candidate_episodes(self, user_id: int, limit: int = 120) -> List[Episode]:
        uid = str(user_id)
        with self._lock:
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
        now = now_ts()

        scored: List[Tuple[float, Episode]] = []
        for ep in candidates:
            blob = f"{ep.text} {ep.tags}".lower()
            ewords = set(_norm_words(blob))
            overlap = len(qwords.intersection(ewords))
            if overlap <= 0:
                continue

            age_days = max(0.0, (now - ep.ts) / 86400.0)
            recency = clamp(1.0 - (age_days / 30.0), 0.0, 1.0)

            score = (overlap * 2.0) + (ep.importance * 1.5) + (recency * 1.0)
            scored.append((score, ep))

        scored.sort(key=lambda x: x[0], reverse=True)
        picked = [ep for _, ep in scored[: int(limit)]]

        # Track usage so frequently recalled episodes become more salient over time.
        if picked:
            with self._lock:
                cur = self.conn.cursor()
                for ep in picked:
                    cur.execute(
                        "UPDATE episodes SET times_used = times_used + 1, last_used_ts = ? WHERE id = ?",
                        (now_ts(), int(ep.id)),
                    )
                self.conn.commit()

        return picked

    def retrieve_relevant_vector(
        self,
        user_id: int,
        query: str,
        limit: int = 6,
        similarity_threshold: float = 0.3,
        recency_weight: float = 0.3,
    ) -> List[Episode]:
        """
        Retrieve relevant episodes using vector similarity.
        
        Falls back to keyword-based retrieve_relevant() if vectors unavailable.
        
        Args:
            user_id: The user to retrieve memories for
            query: The query text to find similar memories
            limit: Maximum number of episodes to return
            similarity_threshold: Minimum cosine similarity (0-1)
            recency_weight: How much to weight recency vs similarity (0-1)
        
        Returns:
            List of relevant Episode objects
        """
        query = (query or "").strip()
        if not query:
            return []

        mv = _get_vector_module()
        if not mv:
            # Fall back to keyword search
            return self.retrieve_relevant(user_id, query, limit)

        # Embed the query
        query_embedding = mv.embed_text(query)
        if not query_embedding:
            # Ollama might be down, fall back
            return self.retrieve_relevant(user_id, query, limit)

        uid = str(user_id)
        now = now_ts()

        # Fetch all episodes with embeddings for this user
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                """
                SELECT id, user_id, ts, text, tags, importance, embedding
                FROM episodes
                WHERE user_id = ? AND embedding IS NOT NULL
                ORDER BY ts DESC
                LIMIT 500
                """,
                (uid,),
            )
            rows = cur.fetchall()

        if not rows:
            # No embedded episodes, fall back
            return self.retrieve_relevant(user_id, query, limit)

        # Build candidates list
        candidates = [(int(row["id"]), row["embedding"]) for row in rows if row["embedding"]]

        # Find similar
        similar = mv.find_similar(
            query_embedding,
            candidates,
            top_k=limit * 2,  # Get extra for hybrid scoring
            threshold=similarity_threshold,
        )

        if not similar:
            return self.retrieve_relevant(user_id, query, limit)

        # Build a map of id -> row for quick lookup
        row_map = {int(r["id"]): r for r in rows}

        # Hybrid scoring: combine similarity with recency
        scored: List[Tuple[float, Episode]] = []
        for ep_id, sim_score in similar:
            row = row_map.get(ep_id)
            if not row:
                continue

            age_days = max(0.0, (now - int(row["ts"])) / 86400.0)
            recency = clamp(1.0 - (age_days / 30.0), 0.0, 1.0)

            # Hybrid score: mostly similarity, some recency
            hybrid = (sim_score * (1 - recency_weight)) + (recency * recency_weight)

            ep = Episode(
                id=int(row["id"]),
                user_id=str(row["user_id"]),
                ts=int(row["ts"]),
                text=str(row["text"]),
                tags=str(row["tags"]),
                importance=float(row["importance"]),
            )
            scored.append((hybrid, ep))

        scored.sort(key=lambda x: x[0], reverse=True)
        picked = [ep for _, ep in scored[:limit]]

        # Track usage
        if picked:
            with self._lock:
                cur = self.conn.cursor()
                for ep in picked:
                    cur.execute(
                        "UPDATE episodes SET times_used = times_used + 1, last_used_ts = ? WHERE id = ?",
                        (now_ts(), int(ep.id)),
                    )
                self.conn.commit()

        return picked

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
        content = self.redact((content or "").strip())
        if role not in {"user", "assistant", "system"}:
            role = "user"
        if not content:
            return

        ts = int(ts or now_ts())
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(
                "INSERT INTO messages(user_id, ts, role, content) VALUES(?, ?, ?, ?)",
                (uid, ts, role, content),
            )
            self.conn.commit()

        self._msg_counter[uid] = self._msg_counter.get(uid, 0) + 1

    def get_recent_messages(self, user_id: int, limit: int = 12) -> List[Dict[str, str]]:
        uid = str(user_id)
        with self._lock:
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

        # Keep the DB bounded.
        try:
            self.prune(user_id)
        except Exception:
            pass

    # -------------------------
    # Prompt injection
    # -------------------------

    def build_prompt_injection(
        self,
        user_id: int,
        user_query: str,
        max_episodes: int = 6,
        use_vector: bool = True,
    ) -> str:
        """Build memory context for prompt injection.
        
        Args:
            user_id: The user to get memories for
            user_query: The current query to find relevant memories
            max_episodes: Maximum episodes to include
            use_vector: If True, use semantic vector search (falls back to keyword if unavailable)
        """
        if use_vector:
            episodes = self.retrieve_relevant_vector(user_id, user_query, limit=max_episodes)
        else:
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
