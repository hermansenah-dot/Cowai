"""
emotion.py

A tiny, deterministic mood engine for steering your chatbot's tone.

Design goals:
- Simple integer mood value (fast + predictable for Discord bots)
- Drift back toward neutral over time
- Provide a prompt-safe mood description (system injection)
- Easy to expand or swap to per-user emotion later

Mood scale (default):
  -3 = furious
  -2 = irritated
  -1 = cold
   0 = neutral
  +1 = friendly
  +2 = upbeat
  +3 = playful
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class MoodState:
    """A single mood state description used for prompt injection."""
    label: str
    guidance: str


class EmotionEngine:
    """
    Discrete mood engine backed by an integer range.

    Typical usage:
        delta = analyze_input(user_text)   # -2..+2
        emotion.apply(delta)
        system_prompt = persona_with_emotion(emotion.description())
        emotion.decay()  # drift toward neutral
    """

    def __init__(self, min_mood: int = -3, max_mood: int = 3, start: int = 0):
        self._min = int(min_mood)
        self._max = int(max_mood)
        self._value = self._clamp(int(start))

        # Central mood table (easy to tweak)
        self._moods: Dict[int, MoodState] = {
            -3: MoodState("furious", "Very annoyed. Short, blunt replies. Sharp sarcasm. Minimal emojis."),
            -2: MoodState("irritated", "Irritated and impatient. A hint of sarcasm. Keep it short."),
            -1: MoodState("cold", "Colder and more distant. Dry tone. Minimal fluff."),
             0: MoodState("neutral", "Calm and neutral. Clear and direct."),
             1: MoodState("friendly", "Friendly and engaged. Light warmth."),
             2: MoodState("upbeat", "Upbeat and expressive. Playful tone."),
             3: MoodState("playful", "Playful and energetic. Emojis are okay, but don't spam."),
        }

    # -------------------------
    # Core operations
    # -------------------------

    def _clamp(self, value: int) -> int:
        return max(self._min, min(value, self._max))

    def apply(self, delta: int) -> None:
        """Apply a mood delta (e.g. from triggers)."""
        self._value = self._clamp(self._value + int(delta))

    def set(self, value: int) -> None:
        """Force mood to a specific value (rarely needed)."""
        self._value = self._clamp(int(value))

    def reset(self) -> None:
        """Reset mood to neutral."""
        self._value = 0

    def decay(self, step: int = 1) -> None:
        """
        Drift slowly toward neutral.

        step=1 is typical (one level per message cycle).
        """
        step = max(1, int(step))

        if self._value > 0:
            self._value = max(0, self._value - step)
        elif self._value < 0:
            self._value = min(0, self._value + step)

    # -------------------------
    # Introspection / prompt text
    # -------------------------

    def value(self) -> int:
        """Current mood value."""
        return self._value

    def label(self) -> str:
        """Short label for logs/debugging."""
        return self._moods.get(self._value, MoodState("unknown", "")).label

    def description(self) -> str:
        """
        Prompt-safe description for system injection.
        Keep this as instructions, not roleplay text.
        """
        state = self._moods.get(self._value)
        if not state:
            return "Mood: neutral. Guidance: Calm and neutral. Clear and direct."
        return f"Mood: {state.label}. Guidance: {state.guidance}"


# Global singleton (simple projects).
# For per-user emotion later: create EmotionEngine per user_id instead of using this.
emotion = EmotionEngine()
