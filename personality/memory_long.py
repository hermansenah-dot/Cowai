from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from memory_sqlite import SQLiteMemory


def _first_meaningful_word(value: str) -> str:
    """Return a single 'keyword' token from a preference phrase.

    One-word mode, but tries to keep the *head* noun when the phrase is a compound:

      - "tea tho" -> "tea"
      - "the coffee" -> "coffee"
      - "video games" -> "games"     (head noun / last meaningful token)
      - "dark chocolate" -> "chocolate"
      - "cats and dogs" -> "cats"    (takes first chunk before a connector)
      - "and hate" -> ""             (filtered out)
    """
    if not value:
        return ""

    # Normalize punctuation to spaces, keep apostrophes inside words
    v = re.sub(r"[\n\t\r]+", " ", value).strip().lower()
    # Note: keep both straight quotes and curly quotes in the strip set.
    v = re.sub(r'[.,!?;:()\[\]{}<>"“”‘’]+', " ", v)
    v = re.sub(r"\s+", " ", v).strip()
    if not v:
        return ""

    leading_stop = {
        "a", "an", "the", "some", "any", "my", "your", "our", "their", "his", "her",
        "and", "or", "but",
        "to", "of", "for", "in", "on", "at", "with",
    }

    # Tokens that commonly indicate the user is adding commentary, not part of the thing they like/dislike
    clause_breakers = {
        "and", "or", "but", "&",
        "tho", "though", "however",
        "because", "since", "cuz",
        "unless", "until", "while", "when",
    }

    trailing_fillers = {
        "tho", "though", "lol", "lmao", "rofl", "tbh", "btw", "rn", "ngl", "idk",
        "pls", "please", "fr", "lmk", "imo", "imho",
        "too", "also", "anyway",
    }

    parts = [p for p in v.split(" ") if p]

    # Drop leading stopwords
    while parts and parts[0] in leading_stop:
        parts.pop(0)

    # If the phrase starts with a connector or verb fragment, bail
    if not parts:
        return ""

    # Cut at the first clause breaker that appears after the first token
    for i, tok in enumerate(parts[1:], start=1):
        if tok in clause_breakers:
            parts = parts[:i]
            break

    # Drop trailing filler tokens (e.g., "tea tho", "coffee lol")
    while parts and parts[-1] in trailing_fillers:
        parts.pop()

    # Drop trailing stopwords just in case
    while parts and parts[-1] in leading_stop:
        parts.pop()

    if not parts:
        return ""

    # Pick the head noun (last meaningful token) for compounds like "video games"
    token = parts[-1].strip("'")

    # Reject tokens that are obviously not a preference target
    if token in {"like", "love", "hate", "dislike"}:
        return ""
    if token in leading_stop or token in clause_breakers or token in trailing_fillers:
        return ""
    if len(token) < 2:
        return ""

    return token


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
        t = (text or "").strip()
        tl = t.lower()

        # Block meta questions like: "Would you like to know what I love and hate?"
        if "?" in t and (_META_PREFERENCE_QUESTION_RE.search(t) or _LOVE_HATE_PAIR_RE.search(tl)):
            return False

        # One-off feedback like "I liked that" should not become long-term "likes".
        # (If the user explicitly signals durable intent, the vague-case resolver below can still store it.)
        if self._is_one_off_feedback(tl):
            return False

        # Detect common preference statements (present tense), allowing up to 3 filler words
        # between "I" and the preference verb (e.g., "I really fucking love coffee").
        patterns = [
            r"\bi\s+(?P<pre>(?:[\w']+\s+){0,3})?like\s+(?P<val>.+)$",
            r"\bi\s+(?P<pre>(?:[\w']+\s+){0,3})?love\s+(?P<val>.+)$",
            r"\bi\s+(?P<pre>(?:[\w']+\s+){0,3})?enjoy\s+(?P<val>.+)$",
            r"\bi\s+(?P<pre>(?:[\w']+\s+){0,3})?prefer\s+(?P<val>.+)$",
            r"\bi(?:'|\s+a)m\s+into\s+(?P<val>.+)$",
            r"\bi\s+am\s+into\s+(?P<val>.+)$",
            r"\bmy\s+favou?rite\b[^\n]{0,32}\b(?:is|are)\s+(?P<val>.+)$",
        ]

        m = None
        for p in patterns:
            m = re.search(p, t, re.IGNORECASE)
            if m:
                break
        if not m:
            return False

        # If the user said something like "I don't really like X", don't store it as a like.
        pre = (m.groupdict().get("pre") or "").lower()
        if re.search(r"\b(don't|do\s+not|didn't|did\s+not|can't|cannot|not|never)\b", pre):
            return False

        value = (m.group("val") or "").strip().strip(" .!?:;\n\t").lower()
        if not value:
            return False

        # Reject connective-only fragments like "and hate" / "or dislike"
        if _CONNECTIVE_ONLY_VALUE_RE.match(value):
            return False

        # Reject values that start with a connective + a preference verb (usually indicates meta phrasing)
        if re.match(r"^(and|or)\s+(hate|hated|dislike|disliked|love|loved|like|liked|enjoy|enjoyed|prefer|preferred)\b", value):
            return False

        # Reject vague values unless the user signals durable intent, then resolve from context.
        if _VAGUE_VALUE_RE.match(value):
            if not self._has_durable_intent(tl):
                return False
            resolved = self._resolve_referent_from_context(prefer_role="assistant", window=10)
            if not resolved:
                return False
            value = resolved

        # Avoid saving very session-specific likes unless the user explicitly says it's durable.
        # Example: "I like this answer" is usually feedback, not a stable preference.
        if not self._has_durable_intent(tl):
            if re.search(r"\b(this|that)\s+(answer|response|message|idea|suggestion|one)\b", value, re.IGNORECASE):
                return False
        # Store only one word (keyword) for long-term memory
        value = _first_meaningful_word(value)
        if not value:
            return False

        # Avoid storing junk / ultra-short values
        if len(value) < 2:
            return False
        likes = _list_from_any(self.data.get("likes"))
        if value and value not in likes:
            likes.append(value)
            self.data["likes"] = likes
            return True
        return False

    def _extract_dislikes(self, text: str) -> bool:
        t = (text or "").strip()
        tl = t.lower()

        # Block meta questions like: "Would you like to know what I love and hate?"
        if "?" in t and (_META_PREFERENCE_QUESTION_RE.search(t) or _LOVE_HATE_PAIR_RE.search(tl)):
            return False

        # Capture negative preferences more broadly than just "i hate" / "i dislike".
        # Supports:
        # - "I hate X" / "I fucking hate X"
        # - "I dislike X"
        # - "I can't stand X"
        # - "I don't like X" / "I do not like X"
        patterns = [
            r"\bi\s+(?P<pre>(?:[\w']+\s+){0,3})?(?:dislike|disliked|hate|hated|can't\s+stand|cannot\s+stand)\s+(?P<val>.+)$",
            r"\bi\s+(?P<pre>(?:[\w']+\s+){0,3})?(?:don't|do\s+not|can't|cannot)\s+(?P<mid>(?:[\w']+\s+){0,3})?like\s+(?P<val>.+)$",
        ]

        m = None
        used_pattern = None
        for p in patterns:
            m = re.search(p, t, re.IGNORECASE)
            if m:
                used_pattern = p
                break
        if not m:
            return False

        pre = (m.groupdict().get("pre") or "").lower()
        mid = (m.groupdict().get("mid") or "").lower()

        # If it's of the form "I don't hate X" / "I never hated X", ignore.
        if used_pattern == patterns[0] and re.search(r"\b(don't|do\s+not|not|never)\b", pre):
            return False

        # For the "don't like" pattern, ensure it's actually negative (it is by construction),
        # but if someone says "I don't really like..." that's still a dislike.
        value = (m.group("val") or "").strip().strip(" .!?:;\n\t").lower()
        if not value:
            return False

        # Reject connective-only fragments like "and love" / "or like"
        if _CONNECTIVE_ONLY_VALUE_RE.match(value):
            return False

        # Reject vague values unless the user signals durable intent, then resolve from context.
        if _VAGUE_VALUE_RE.match(value):
            if not self._has_durable_intent(tl):
                return False
            resolved = self._resolve_referent_from_context(prefer_role="assistant", window=10)
            if not resolved:
                return False
            value = resolved
        # Store only one word (keyword) for long-term memory
        value = _first_meaningful_word(value)
        if not value:
            return False

        # Avoid storing junk / ultra-short values
        if len(value) < 2:
            return False
        dislikes = _list_from_any(self.data.get("dislikes"))
        if value and value not in dislikes:
            dislikes.append(value)
            self.data["dislikes"] = dislikes
            return True
        return False

    # ------------------
    # Helper gates (natural memory)
    # ------------------

    def _is_one_off_feedback(self, tl: str) -> bool:
        """Return True for short-lived reactions that shouldn't become durable likes/dislikes."""
        if not tl:
            return False
        # Past-tense reactions are usually feedback about the current convo item
        if _ONE_OFF_FEEDBACK_RE.search(tl):
            return True
        # Generic praise without an object ("that was great") is not a stable preference
        if re.search(r"\bthat\s+(was|is)\s+(nice|good|great|cool|awesome|amazing)\b", tl):
            return True
        return False

    def _has_durable_intent(self, tl: str) -> bool:
        """Return True when the user signals they want this remembered going forward."""
        return bool(tl and _DURABLE_INTENT_RE.search(tl))

    def _resolve_referent_from_context(self, prefer_role: str = "assistant", window: int = 10) -> Optional[str]:
        """Resolve vague 'that/this/it' by looking at recent messages in SQLite."""
        try:
            uid = int(self.user_id)
        except Exception:
            return None

        try:
            msgs = _SQL.get_recent_messages(uid, limit=max(3, int(window)))
        except Exception:
            return None

        if not msgs:
            return None

        # Prefer the latest message from prefer_role, else fall back to the latest non-empty message
        content = ""
        for m in reversed(msgs):
            if (m.get("role") == prefer_role) and (m.get("content") or "").strip():
                content = (m.get("content") or "").strip()
                break
        if not content:
            for m in reversed(msgs):
                if (m.get("content") or "").strip():
                    content = (m.get("content") or "").strip()
                    break
        if not content:
            return None

        # Heuristic: last bullet/line is often the concrete suggestion/item the user is referring to
        raw_lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        bullet_lines = [ln.lstrip("-• \t").strip() for ln in raw_lines if ln.lstrip().startswith(("-", "•"))]
        if bullet_lines:
            candidate = bullet_lines[-1]
        else:
            # Fall back to last sentence-ish chunk
            parts = re.split(r"(?<=[.!?])\s+", content)
            parts = [p.strip() for p in parts if p.strip()]
            candidate = parts[-1] if parts else content

        candidate = candidate.strip(" \t\n\r""'`“”‘’.,!?:;()").lower()
        if not candidate or len(candidate) < 3:
            return None

        # Avoid returning pure role markers or obvious template artifacts
        if re.match(r"^(user|assistant|system)\s*:\s*$", candidate):
            return None

        return candidate

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
            # Be very explicit to avoid confusion with the bot's own name
            parts.append(f"IMPORTANT: The person you are talking to wants to be called \"{self.data['name']}\" (not your name - YOUR name is mAIcé, THEIR name is {self.data['name']}).")
        parts.append(f"Preferred language: {self.data.get('preferred_language', 'English')}.")
        likes = _list_from_any(self.data.get("likes"))
        dislikes = _list_from_any(self.data.get("dislikes"))
        if likes:
            parts.append("The user likes: " + ", ".join(likes) + ".")
        if dislikes:
            parts.append("The user dislikes: " + ", ".join(dislikes) + ".")
        return " ".join(parts).strip()

# Meta questions about preferences (not actual stated preferences), e.g.:
# "Would you like to know what I love and hate?"
_META_PREFERENCE_QUESTION_RE = re.compile(
    r"\b(would you like to know|do you want to know|want to know|"
    r"would you like me to tell you|should i tell you|can i tell you)\b",
    re.IGNORECASE,
)

_LOVE_HATE_PAIR_RE = re.compile(
    r"\bwhat\s+i\b.*\b(like|love|enjoy|prefer)\b.*\b(and|or)\b.*\b(hate|dislike|can't stand|cannot stand|don't like|do not like)\b",
    re.IGNORECASE,
)

_CONNECTIVE_ONLY_VALUE_RE = re.compile(
    r"^(and|or)\s+(hate|hated|dislike|disliked|love|loved|like|liked|enjoy|enjoyed|prefer|preferred)\b(?:\s+(it|this|that))?\s*$",
    re.IGNORECASE,
)


# Vague referents that require context resolution + durable intent to store.
_VAGUE_VALUE_RE = re.compile(r"^(?:that|this|it|this\s+one|that\s+one)\s*$", re.IGNORECASE)

# Phrases that indicate the user wants this preference remembered for future chats.
_DURABLE_INTENT_RE = re.compile(
    r"\b(from now on|going forward|remember( this)?|note this|do that again|do this again|"
    r"stick with that|stick with this|in the future|next time)\b",
    re.IGNORECASE,
)

# One-off feedback/reactions that should not become long-term preferences.
_ONE_OFF_FEEDBACK_RE = re.compile(
    r"\b(i\s+(?:really\s+)?)?(liked|loved|enjoyed)\b\s+(that|this|it)\b|"
    r"\b(that|this|it)\s+(was|is)\s+(nice|good|great|cool|awesome|amazing)\b",
    re.IGNORECASE,
)
