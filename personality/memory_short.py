from .persona import persona_with_emotion
from emotion import emotion
from tools import get_current_time

MAX_MESSAGES = 12


class ShortTermMemory:
    def __init__(self):
        # System message is always index 0
        self.messages = [{"role": "system", "content": ""}]
        self._system_extras: list[str] = []
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
        time_info = (
            f"Current real-world time: {get_current_time()}.\n"
            "If the user asks for the time, answer using this value."
        )

        base = (
            persona_with_emotion(emotion.description())
            + "\n\nIMPORTANT:\n"
            + "Respond in English only. Do not switch languages."
            + "\n\n"
            + time_info
        )

        if self._system_extras:
            base += "\n\n" + "\n\n".join(self._system_extras)

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
