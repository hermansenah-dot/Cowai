"""emotion.py

Deterministic affect/mood engine for steering the bot's tone.

This version is intentionally *more realistic* than a single integer mood:
- Tracks a small affect vector: valence, arousal, dominance (VAD).
- Uses inertia (smoothing) so emotions don't flip instantly.
- Uses time-based decay (exponential-ish) toward neutral.
- Maintains both a fast-changing "emotion" layer and a slow-changing
    "mood baseline" layer.

Important: it is still a GLOBAL singleton in this project (no per-user state).
The public API remains compatible with earlier code:
- apply(delta)
- decay(...)
- value()/to_int()
- label()
- description()
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Any, Dict


@dataclass
class Affect:
    """A small affect vector.

    Ranges are expected to stay within [-1.0, 1.0] after clamping.
    """

    valence: float = 0.0    # unpleasant (-) <-> pleasant (+)
    arousal: float = 0.0    # calm (-) <-> activated (+)
    dominance: float = 0.0  # powerless (-) <-> in-control (+)


class EmotionEngine:
    """Affect engine with a fast emotion layer + slow mood baseline.

    It is deterministic and light (good for bots), but less "binary" than an
    integer-only mood.
    """

    def __init__(self):
        # Fast-changing "current emotion" (reacts to the last few messages)
        self._emotion = Affect()

        # Slow-changing baseline (drifts slowly based on recent emotion)
        self._baseline = Affect()

        # For time-based decay
        self._last_update = time.time()

        # Guidance table (small & readable)
        self._guidance: Dict[str, str] = {
            "furious": "Very annoyed. Short, blunt replies. Avoid emojis.",
            "irritated": "Irritated and impatient. Keep it brief; avoid rambling.",
            "tense": "Slightly tense. Be direct and helpful; de-escalate.",
            "cold": "Colder and more distant. Dry tone, minimal fluff.",
            "neutral": "Calm and neutral. Clear and direct.",
            "calm": "Calm and steady. Helpful and grounded.",
            "friendly": "Friendly and engaged. Light warmth.",
            "upbeat": "Upbeat and expressive. A little playful is OK.",
            "playful": "Playful and energetic. Emojis are OK, but don't spam.",
        }

    # -------------------------
    # Core ops
    # -------------------------

    @staticmethod
    def _clampf(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
        return max(lo, min(hi, float(x)))

    def _clamp_affect(self, a: Affect) -> None:
        a.valence = self._clampf(a.valence)
        a.arousal = self._clampf(a.arousal)
        a.dominance = self._clampf(a.dominance)

    @staticmethod
    def _mix(current: float, target: float, alpha: float) -> float:
        """Inertia/smoothing: alpha=0 keeps current, alpha=1 jumps to target."""
        alpha = max(0.0, min(1.0, float(alpha)))
        return (1.0 - alpha) * current + alpha * target

    def apply(self, delta: int | Dict[str, Any]) -> None:
        """Apply an affect delta.

        Backwards compatible:
        - If `delta` is an int (legacy triggers), it mainly affects valence,
          with a small arousal bump for big magnitude.
        - If `delta` is a dict, supports keys: valence/arousal/dominance
          (floats in [-1..1]) and optional confidence in [0..1].
        """
        # Update time-based decay before applying new input
        self.decay()

        if isinstance(delta, dict):
            dv = float(delta.get("valence", 0.0) or 0.0)
            da = float(delta.get("arousal", 0.0) or 0.0)
            dd = float(delta.get("dominance", 0.0) or 0.0)
            conf = float(delta.get("confidence", 1.0) or 1.0)
            conf = max(0.0, min(1.0, conf))
        else:
            # Legacy scale: expected about -2..+2, sometimes wider.
            n = float(int(delta))
            dv = n / 3.0
            da = min(0.35, abs(n) / 6.0)
            dd = 0.05 if n > 0 else (-0.05 if n < 0 else 0.0)
            conf = 1.0

        # Inertia: don't jump instantly.
        # Faster response when confidence is high.
        alpha = 0.45 * conf + 0.10

        self._emotion.valence = self._mix(self._emotion.valence, self._emotion.valence + dv, alpha)
        self._emotion.arousal = self._mix(self._emotion.arousal, self._emotion.arousal + da, alpha)
        self._emotion.dominance = self._mix(self._emotion.dominance, self._emotion.dominance + dd, alpha)
        self._clamp_affect(self._emotion)

        # Very slow baseline learning (a rolling average of recent emotion)
        base_alpha = 0.04 * conf
        self._baseline.valence = self._mix(self._baseline.valence, self._emotion.valence, base_alpha)
        self._baseline.arousal = self._mix(self._baseline.arousal, self._emotion.arousal, base_alpha)
        self._baseline.dominance = self._mix(self._baseline.dominance, self._emotion.dominance, base_alpha)
        self._clamp_affect(self._baseline)

    def set(self, value: int) -> None:
        """Force the *overall* mood value (legacy API).

        This maps the integer into baseline valence (and a bit of arousal).
        """
        v = max(-3, min(3, int(value)))
        self._baseline.valence = v / 3.0
        self._baseline.arousal = 0.15 if v != 0 else 0.0
        self._baseline.dominance = 0.0
        self._emotion = Affect()  # reset spikes
        self._clamp_affect(self._baseline)

    def reset(self) -> None:
        """Reset back to neutral."""
        self._emotion = Affect()
        self._baseline = Affect()
        self._last_update = time.time()

    def decay(self, step: int = 1) -> None:
        """Time-based decay toward neutral.

        `step` is kept for API compatibility; higher values increase decay.
        """
        now = time.time()
        dt = max(0.0, now - self._last_update)
        self._last_update = now

        # Fast layer decays quicker than baseline.
        # These are tuned to feel stable in a chat loop.
        step = max(1, int(step))
        k_fast = 1.2 * step
        k_slow = 0.25 * step

        # Exponential-ish decay: x <- x * exp(-k * dt)
        fast_factor = math.exp(-k_fast * dt / 30.0)   # ~30s time constant
        slow_factor = math.exp(-k_slow * dt / 300.0)  # ~5min time constant

        self._emotion.valence *= fast_factor
        self._emotion.arousal *= fast_factor
        self._emotion.dominance *= fast_factor

        self._baseline.valence *= slow_factor
        self._baseline.arousal *= slow_factor
        self._baseline.dominance *= slow_factor

        self._clamp_affect(self._emotion)
        self._clamp_affect(self._baseline)

    # -------------------------
    # Introspection / prompt text
    # -------------------------

    def _overall_valence(self) -> float:
        # Baseline dominates; emotion spikes can push it a bit.
        return self._clampf(self._baseline.valence + 0.65 * self._emotion.valence)

    def _overall_arousal(self) -> float:
        return self._clampf(self._baseline.arousal + 0.75 * self._emotion.arousal)

    def value(self) -> int:
        """Legacy mood integer in [-3..3] derived from overall valence."""
        v = self._overall_valence()
        return int(round(3.0 * v))

    # Compatibility helpers (older bot code)
    def to_int(self) -> int:
        return self.value()

    @property
    def mood(self) -> int:
        return self.value()

    def label(self) -> str:
        """Human-friendly label derived from overall V/A."""
        v = self._overall_valence()
        a = self._overall_arousal()

        if v <= -0.80 and a >= 0.40:
            return "furious"
        if v <= -0.55 and a >= 0.25:
            return "irritated"
        if v <= -0.25 and a >= 0.15:
            return "tense"
        if v <= -0.25 and a < 0.15:
            return "cold"

        if v >= 0.75 and a >= 0.25:
            return "playful"
        if v >= 0.45 and a >= 0.20:
            return "upbeat"
        if v >= 0.25 and a < 0.20:
            return "friendly"

        # Neutral-ish: arousal can still describe the vibe
        if abs(v) < 0.18 and a < 0.10:
            return "neutral"
        if a < 0.10:
            return "calm"
        return "neutral"

    def description(self) -> str:
        """
        Prompt-safe description for system injection.
        Keep this as instructions, not roleplay text.
        """
        label = self.label()
        guidance = self._guidance.get(label, self._guidance["neutral"])

        # Expose a small amount of numeric context (helps the system prompt be stable).
        v = self._overall_valence()
        a = self._overall_arousal()
        d = self._clampf(self._baseline.dominance + 0.50 * self._emotion.dominance)

        return (
            f"Mood: {label}. Guidance: {guidance} "
            f"(valence={v:+.2f}, arousal={a:+.2f}, dominance={d:+.2f})"
        )

    def metrics(self) -> Dict[str, float]:
        """Numeric snapshot for logs/telemetry.

        Keys:
        - valence/arousal/dominance: overall values in [-1..1]
        - baseline_*: slow baseline components
        - emotion_*: fast emotion components
        """
        return {
            "valence": self._overall_valence(),
            "arousal": self._overall_arousal(),
            "dominance": self._clampf(self._baseline.dominance + 0.50 * self._emotion.dominance),
            "baseline_valence": self._baseline.valence,
            "baseline_arousal": self._baseline.arousal,
            "baseline_dominance": self._baseline.dominance,
            "emotion_valence": self._emotion.valence,
            "emotion_arousal": self._emotion.arousal,
            "emotion_dominance": self._emotion.dominance,
        }


# Global singleton.
emotion = EmotionEngine()
