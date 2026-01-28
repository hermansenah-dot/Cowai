from .persona import persona_with_emotion
from emotion import emotion
from tools import get_current_time

MAX_MESSAGES = 12


class ShortTermMemory:
    def __init__(self):
        self.messages = [{"role": "system", "content": ""}]
        self.refresh_system()

    def refresh_system(self):
        """Update the system message with persona, emotion and current time."""
        time_info = (
            f"Current real-world time: {get_current_time()}.\n"
            "If the user asks for the time, answer using this value."
        )

        #self.messages[0]["content"] = (
        #    persona_with_emotion(emotion.description())
        #    + "\n\n"
        #    + time_info
        #)

        self.messages[0]["content"] = (
            persona_with_emotion(emotion.description())
            + "\n\nIMPORTANT:\n"
            + "Respond in English only. Do not switch languages."
            + "\n\n"
            + time_info
        )
    def add(self, role, content):
        """Add a new message to memory, keeping only the most recent MAX_MESSAGES."""
        self.messages.append({"role": role, "content": content})

        if len(self.messages) > MAX_MESSAGES:
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
