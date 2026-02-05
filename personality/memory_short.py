from personality.persona import persona_with_emotion
from core.mood import emotion
from utils.helpers import get_current_time

try:
    from config.config import EMOTION_ENABLED
except ImportError:
    EMOTION_ENABLED = True  # Default to enabled if config missing

MAX_MESSAGES = 8

# Cache the base persona to avoid regenerating it every call
_PERSONA_CACHE: tuple[str | None, str] | None = None  # (emotion_desc, result)


class ShortTermMemory:
    def __init__(self):
        # System message is always index 0
        self.messages = [{"role": "system", "content": ""}]
        self._system_extras: list[str] = []
        self._last_system_hash: int = 0  # Track if we need to rebuild
        self.refresh_system()

    def _ensure_system_message(self) -> None:
        if not self.messages:
            self.messages = [{"role": "system", "content": ""}]
            return

        first = self.messages[0]
        if not isinstance(first, dict) or first.get("role") != "system":
            self.messages.insert(0, {"role": "system", "content": ""})

    def set_system_extras(self, extras) -> None:
        """Set additional system blocks appended after the base system prompt."""
        if not extras:
            self._system_extras = []
            return

        cleaned: list[str] = []
        for x in extras:
            s = str(x).strip()
            if s:
                cleaned.append(s)

        self._system_extras = cleaned

    def refresh_system(self):
        """Update the system message with persona, emotion and current time."""
        global _PERSONA_CACHE
        
        # Get emotion description
        emotion_desc = emotion.description() if EMOTION_ENABLED else None
        
        # Use cached persona if emotion unchanged
        if _PERSONA_CACHE and _PERSONA_CACHE[0] == emotion_desc:
            base_persona = _PERSONA_CACHE[1]
        else:
            base_persona = persona_with_emotion(emotion_desc)
            _PERSONA_CACHE = (emotion_desc, base_persona)
        
        # Build time info (changes every call, but simple string)
        time_info = f"Current real-world time: {get_current_time()}."
        
        # Assemble system prompt
        parts = [
            base_persona,
            "\nIMPORTANT:\nRespond in English only. Do not switch languages.",
            f"\n{time_info}\nIf the user asks for the time, answer using this value.",
        ]
        
        if self._system_extras:
            parts.append("\n\n" + "\n\n".join(self._system_extras))
        
        base = "".join(parts)

        self._ensure_system_message()
        self.messages[0]["content"] = base

    def hydrate_from_history(self, history, max_messages: int = MAX_MESSAGES):
        """Rebuild short-term memory from persistent history (e.g., SQLite) after a reboot.

        Expected input:
            history: list of dicts like {"role": "user"/"assistant", "content": "..."}
                     in chronological order (oldest -> newest)

        Behavior:
        - Keeps the current system message (index 0)
        - Appends up to (max_messages - 1) recent turns
        - Ignores any 'system' items in history
        """
        if not history:
            return

        cleaned = []
        for m in history:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role", "")).strip().lower()
            content = str(m.get("content", "")).strip()
            if role not in ("user", "assistant"):
                continue
            if not content:
                continue
            cleaned.append({"role": role, "content": content})

        if not cleaned:
            return

        keep = max(1, int(max_messages) - 1)
        cleaned = cleaned[-keep:]

        # Preserve system message, replace the rest with hydrated history
        self.messages = [self.messages[0]] + cleaned

    def add(self, role: str, content: str):
        """Add a new message to memory, keeping only the most recent MAX_MESSAGES."""
        self.messages.append({"role": role, "content": content})

        if len(self.messages) > MAX_MESSAGES:
            # Keep system + last (MAX_MESSAGES - 1) turns
            self.messages = [self.messages[0]] + self.messages[-(MAX_MESSAGES - 1):]

    def get_messages(self):
        """Return chat messages for Ollama /api/chat."""
        # Always refresh system right before using it
        self.refresh_system()
        return self.messages


# -------------------------
# Per-user short-term memory
# -------------------------

short_memories = {}  # user_id -> ShortTermMemory


def get_short_memory(user_id):
    if user_id not in short_memories:
        short_memories[user_id] = ShortTermMemory()
    return short_memories[user_id]
