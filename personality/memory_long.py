from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from memory_sqlite import SQLiteMemory


# Store per-user JSON snapshots here (kept for backwards compatibility/debugging).
# project_root/memory/users/<user_id>.json
BASE_DIR = Path("memory") / "users"

# SQLite DB for structured memory (facts + episodes + message log)
_SQL = SQLiteMemory(db_path=str(Path("memory") / "memory.db"))


def _bool_from_any(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    return s in {"1", "true", "yes", "y", "on", "enabled"}


def _list_from_any(v: Any) -> List[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        # Stored as CSV-like string in SQLite facts
        parts = [p.strip() for p in v.split(",")]
        return [p for p in parts if p]
    return []


class Long_Term_Memory:
    """Per-user long-term memory.

    Current behavior:
    - Uses SQLite for structured facts + episodic memories + recent message log.
    - Keeps a JSON snapshot on disk for compatibility/debugging.

    Compatibility:
    - Exposes .data with keys: name, preferred_language, likes, dislikes, voice_enabled
    - Commands still persist voice_enabled through .data + .save()
    """

    def __init__(self, user_id: int):
        self.user_id = str(user_id)
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        self.file_path = BASE_DIR / f"{self.user_id}.json"

        self.data: Dict[str, Any] = {
            "name": None,
            "preferred_language": "English",
            "likes": [],
            "dislikes": [],
            "voice_enabled": False,
        }

        # Load best-effort from SQLite (preferred), then JSON snapshot as fallback.
        self._load_from_sqlite(int(user_id))
        self.load()

        # Ensure voice_enabled always exists
        if "voice_enabled" not in self.data:
            self.data["voice_enabled"] = False

        # Persist a normalized snapshot
        self.save()

    # ------------------
    # Persistence
    # ------------------

    def _load_from_sqlite(self, user_id: int) -> None:
        try:
            facts = _SQL.get_facts(user_id)
        except Exception:
            return

        if not facts:
            return

        if "name" in facts:
            self.data["name"] = facts.get("name") or None
        if "preferred_language" in facts:
            self.data["preferred_language"] = facts.get("preferred_language") or "English"
        if "likes" in facts:
            self.data["likes"] = _list_from_any(facts.get("likes"))
        if "dislikes" in facts:
            self.data["dislikes"] = _list_from_any(facts.get("dislikes"))
        if "voice_enabled" in facts:
            self.data["voice_enabled"] = _bool_from_any(facts.get("voice_enabled"))

    def load(self) -> None:
        """Load JSON snapshot if present."""
        if self.file_path.exists():
            try:
                loaded = json.loads(self.file_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    # Only accept known keys to prevent prompt injection via JSON file edits.
                    for k in ["name", "preferred_language", "likes", "dislikes", "voice_enabled"]:
                        if k in loaded:
                            self.data[k] = loaded[k]
            except Exception as e:
                print(f"[Memory] Failed to load {self.user_id}: {e}")

        # Normalize types
        self.data["likes"] = _list_from_any(self.data.get("likes"))
        self.data["dislikes"] = _list_from_any(self.data.get("dislikes"))
        self.data["voice_enabled"] = _bool_from_any(self.data.get("voice_enabled", False))

        if not self.data.get("preferred_language"):
            self.data["preferred_language"] = "English"

    def save(self) -> None:
        """Persist to SQLite (structured) and write a JSON snapshot."""
        try:
            uid = int(self.user_id)
        except Exception:
            uid = 0

        # SQLite facts (structured)
        try:
            if uid:
                if self.data.get("name"):
                    _SQL.upsert_fact(uid, "name", str(self.data["name"]).strip(), confidence=0.9)
                if self.data.get("preferred_language"):
                    _SQL.upsert_fact(uid, "preferred_language", str(self.data["preferred_language"]).strip(), confidence=0.9)

                likes = ", ".join(_list_from_any(self.data.get("likes")))
                dislikes = ", ".join(_list_from_any(self.data.get("dislikes")))

                if likes:
                    _SQL.upsert_fact(uid, "likes", likes, confidence=0.7)
                if dislikes:
                    _SQL.upsert_fact(uid, "dislikes", dislikes, confidence=0.7)

                _SQL.upsert_fact(uid, "voice_enabled", "1" if _bool_from_any(self.data.get("voice_enabled")) else "0", confidence=1.0)
        except Exception as e:
            print(f"[Memory] SQLite save failed for {self.user_id}: {e}")

        # JSON snapshot (debug/compat)
        try:
            snap = {
                "name": self.data.get("name"),
                "preferred_language": self.data.get("preferred_language", "English"),
                "likes": _list_from_any(self.data.get("likes")),
                "dislikes": _list_from_any(self.data.get("dislikes")),
                "voice_enabled": _bool_from_any(self.data.get("voice_enabled", False)),
            }

            self.file_path.write_text(
                json.dumps(snap, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[Memory] Failed to save {self.user_id}: {e}")

    # ------------------
    # Message log + extraction
    # ------------------

    def record_message(self, role: str, content: str) -> None:
        """Record a message to the SQLite message log for episodic extraction."""
        try:
            _SQL.add_message(int(self.user_id), role=role, content=content)
        except Exception:
            pass

    def maybe_extract(self, ask_llama_fn) -> None:
        """Run LLM memory extraction every N messages (non-blocking caller recommended)."""
        try:
            uid = int(self.user_id)
        except Exception:
            return
        try:
            if not _SQL.should_extract(uid, every_n_messages=8):
                return
            _SQL.extract_and_store(uid, ask_llama_fn, window=12)
            _SQL.reset_extract_counter(uid)
        except Exception:
            return

    # ------------------
    # Extraction logic (fast, deterministic)
    # ------------------

    def update_from_text(self, text: str) -> None:
        """Update stable facts from user text (fast deterministic rules)."""
        changed = False
        changed |= self._extract_name(text)
        changed |= self._extract_language(text)
        changed |= self._extract_likes(text)
        changed |= self._extract_dislikes(text)
        if changed:
            self.save()

    def _extract_name(self, text: str) -> bool:
        patterns = [
            r"\bmy name is ([A-Za-z'-]+)\b",
            r"\bi am ([A-Za-z'-]+)\b",
            r"\bi'm ([A-Za-z'-]+)\b",
            r"\bcall me ([A-Za-z'-]+)\b",
        ]

        for p in patterns:
            m = re.search(p, text, re.IGNORECASE)
            if m:
                name = m.group(1)
                if self.data.get("name") != name:
                    self.data["name"] = name
                    return True
        return False

    def _extract_language(self, text: str) -> bool:
        t = text.lower()
        if "english" in t:
            return self._set("preferred_language", "English")
        if "danish" in t:
            return self._set("preferred_language", "Danish")
        return False

    def _extract_likes(self, text: str) -> bool:
        t = text.lower()
        if "i like" not in t:
            return False
        value = text.split("i like", 1)[1].strip(" .!").lower()
        likes = _list_from_any(self.data.get("likes"))
        if value and value not in likes:
            likes.append(value)
            self.data["likes"] = likes
            return True
        return False

    def _extract_dislikes(self, text: str) -> bool:
        t = text.lower()
        if "i dislike" in t:
            value = text.split("i dislike", 1)[1]
        elif "i hate" in t:
            value = text.split("i hate", 1)[1]
        else:
            return False

        value = value.strip(" .!").lower()
        dislikes = _list_from_any(self.data.get("dislikes"))
        if value and value not in dislikes:
            dislikes.append(value)
            self.data["dislikes"] = dislikes
            return True
        return False

    def _set(self, key: str, value: Any) -> bool:
        if self.data.get(key) != value:
            self.data[key] = value
            return True
        return False

    # ------------------
    # Prompt injection
    # ------------------

    def as_prompt(self, user_query: Optional[str] = None) -> str:
        """Inject only the most relevant memories + stable facts."""
        try:
            uid = int(self.user_id)
        except Exception:
            uid = 0

        q = (user_query or "").strip()
        if uid and q:
            try:
                return _SQL.build_prompt_injection(uid, q, max_episodes=6)
            except Exception:
                pass

        # Fallback: facts only (old behavior)
        parts: List[str] = []
        if self.data.get("name"):
            parts.append(f"The user's name is {self.data['name']}.")
        parts.append(f"Preferred language: {self.data.get('preferred_language', 'English')}.")
        likes = _list_from_any(self.data.get("likes"))
        dislikes = _list_from_any(self.data.get("dislikes"))
        if likes:
            parts.append("The user likes: " + ", ".join(likes) + ".")
        if dislikes:
            parts.append("The user dislikes: " + ", ".join(dislikes) + ".")
        return " ".join(parts).strip()
