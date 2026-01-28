from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MoodState:
    """A single mood state description used for prompt injection."""
    label: str
    instruction: str


class EmotionEngine:
    """
    Simple discrete mood engine.

    mood range: [-3..3]
      -3 = very angry/annoyed
       0 = neutral
       3 = very upbeat/playful

    This engine is deterministic and lightweight (good for Discord bots).
    """

    def __init__(self, min_mood: int = -3, max_mood: int = 3, start: int = 0):
        self.min_mood = min_mood
        self.max_mood = max_mood
        self.mood = self._clamp(start)

        # Centralized mood table (easy to tweak)
        self._moods: dict[int, MoodState] = {
            -3: MoodState("furious", "Very annoyed. Short, blunt replies. Sharp sarcasm. Minimal emojis."),
            -2: MoodState("irritated", "Irritated and impatient. A hint of sarcasm. Keep it short."),
            -1: MoodState("cold", "Colder and more distant. Dry tone. Minimal fluff."),
             0: MoodState("neutral", "Calm and neutral. Clear and direct."),
             1: MoodState("friendly", "Friendly and engaged. Light warmth."),
             2: MoodState("upbeat", "Upbeat and expressive. Playful tone."),
             3: MoodState("playful", "Playful and energetic. Emojis are okay, but don't spam."),
        }

    def _clamp(self, value: int) -> int:
        return max(self.min_mood, min(value, self.max_mood))

    def apply(self, delta: int) -> None:
        """Apply a mood delta (e.g., from your trigger analyzer)."""
        self.mood = self._clamp(self.mood + int(delta))

    def set(self, value: int) -> None:
        """Force mood to a specific value (rarely needed)."""
        self.mood = self._clamp(int(value))

    def decay(self, step: int = 1) -> None:
        """
        Drift slowly toward neutral.
        step=1 is typical (one level per message cycle).
        """
        step = max(1, int(step))

        if self.mood > 0:
            self.mood = max(0, self.mood - step)
        elif self.mood < 0:
            self.mood = min(0, self.mood + step)

    def label(self) -> str:
        """Short label for logging / debugging."""
        return self._moods.get(self.mood, MoodState("unknown", "")).label

    def description(self) -> str:
        """
        Prompt-safe description for system injection.
        Keep this as instructions, not roleplay text.
        """
        state = self._moods.get(self.mood)
        if not state:
            return "Neutral."
        return f"Mood: {state.label}. Guidance: {state.instruction}"

    def to_int(self) -> int:
        """Current mood value as an integer."""
        return self.mood


# Global singleton (simple projects).
# For per-user emotion later: create EmotionEngine per user_id instead of using this.
emotion = EmotionEngine()
