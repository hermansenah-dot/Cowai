# humanize.py
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Optional, Callable, Any

import json
import os
import sys

# Enable tracing without changing call-sites by setting env var HUMANIZE_TRACE=1
THOUGHT_TRACE_DEFAULT = os.getenv("HUMANIZE_TRACE", "").strip().lower() in ("1", "true", "yes", "on")

# If you want deterministic behavior while testing, set to True
DETERMINISTIC = False

# How often to prepend a listening line (0..1)
LISTENING_RATE = 0.85

# Max one follow-up question, and only when ambiguity is detected
FOLLOWUP_RATE = 0.45

# Never show debug-like listening labels in user-facing text
LISTENING_LABEL_PREFIXES = (
    "listening line:",
    "listening:",
)

def strip_listening_label(s: str) -> str:
    """Remove any accidental 'Listening line:' style prefix from the start of a line."""
    if not s:
        return s
    lines = s.splitlines()
    if not lines:
        return s
    first = lines[0].lstrip()
    low = first.lower()
    for p in LISTENING_LABEL_PREFIXES:
        if low.startswith(p):
            # Drop the prefix and following whitespace/punctuation
            first = re.sub(r"(?i)^listening(\s+line)?\s*:\s*", "", first).lstrip()
            lines[0] = first
            break
    return "\n".join(lines).strip()



def emit_thought(trace: dict[str, Any], logger: Callable[[str], None] | None = None) -> None:
    """Emit a compact one-line JSON 'thought pattern' for debugging humanization decisions.

    This deliberately avoids logging raw user text or the full reply. It logs only the
    internal signals and decisions this module actually uses.
    """
    line = json.dumps(trace, ensure_ascii=False, separators=(",", ":"))
    if logger is not None:
        logger(line)
        return
    # stderr + flush makes it far more likely you actually see it in consoles
    print(line, file=sys.stderr, flush=True)


@dataclass
class Style:
    relax: float = 0.4          # 0..1 from trust style
    mood_label: str = "neutral" # from emotion.label()
    valence: float = 0.0        # from emotion.metrics()
    arousal: float = 0.0
    dominance: float = 0.0


def _rng() -> random.Random:
    if DETERMINISTIC:
        return random.Random(1337)
    return random  # type: ignore[return-value]


def infer_intent(user_text: str) -> str:
    t = (user_text or "").strip().lower()
    if not t:
        return "general"
    if t.startswith(("why", "how come", "what causes")):
        return "explain"
    if t.startswith(("how", "how do i", "how would", "what’s the best way", "whats the best way")):
        return "how_to"
    if t.startswith(("can you", "could you", "please", "add ", "make ", "change ", "update ", "remove ")):
        return "request_change"
    if t.endswith("?"):
        return "question"
    return "general"


def extract_constraints(user_text: str) -> list[str]:
    """Tiny heuristic constraint extractor (fast + good-enough)."""
    t = (user_text or "").lower()
    out: list[str] = []

    # Voice / TTS common constraints (you can expand this list)
    if "p225" in t:
        out.append("keep p225")
    if "deeper" in t or "deep" in t or "lower" in t:
        out.append("make it deeper")
    if "warm" in t or "warmer" in t:
        out.append("make it warmer")

    # Output preference
    if "code" in t and ("not file" in t or "no file" in t or "not files" in t):
        out.append("show code only")

    # Logging preference
    if "same line" in t and ("mood" in t or "console" in t):
        out.append("log in the same console line")

    # De-dup while preserving order
    seen = set()
    uniq: list[str] = []
    for c in out:
        if c not in seen:
            uniq.append(c)
            seen.add(c)
    return uniq


def should_listen(user_text: str) -> bool:
    """Skip listening line for tiny messages to avoid sounding repetitive."""
    t = (user_text or "").strip()
    if len(t) < 6:
        return False
    if re.fullmatch(r"(ok|okay|thx|thanks|ty|nice|cool)\.?!!?\??", t.lower()):
        return False
    return _rng().random() < LISTENING_RATE


def listening_line(user_text: str, style: Style) -> str:
    intent = infer_intent(user_text)
    constraints = extract_constraints(user_text)

    relaxed = style.relax >= 0.60
    low_energy = style.arousal < -0.15
    high_energy = style.arousal > 0.30

    if intent in ("how_to", "request_change"):
        openers = ["Yep—can do.", "Sure.", "Alright.", "Okay, got you."]
    elif intent == "explain":
        openers = ["Yep.", "Got you.", "Makes sense.", "Okay—here’s what’s happening."]
    elif intent == "question":
        openers = ["Yep.", "Yeah.", "Totally.", "For sure."]
    else:
        openers = ["Got it.", "Okay.", "Fair.", "Alright."]

    if relaxed and _rng().random() < 0.35:
        openers += ["Yep.", "No worries.", "Gotcha."]

    opener = _rng().choice(openers)

    if constraints:
        reflected = ", ".join(constraints[:2])
        return f"{opener} So: **{reflected}**."
    if high_energy:
        return f"{opener} Let’s do it."
    if low_energy:
        return f"{opener} One sec."
    return opener



def system_style_block(style: Style) -> str:
    """Short, high-leverage system guidance appended via memory_short extras."""
    if style.relax >= 0.70:
        tone = "Relaxed, casual, chaotic energy allowed."
    elif style.relax >= 0.35:
        tone = "Friendly but direct."
    else:
        tone = "Professional and neutral."

    return f"Reply style: {tone} Match the user's energy - short and snappy for casual, unhinged rants when things get exciting."





def looks_like_it_already_listened(reply: str) -> bool:
    if not reply:
        return False
    first = reply.strip().splitlines()[0].strip()
    return bool(re.match(r"^(yep|yeah|sure|got it|gotcha|okay|alright|for sure|no worries)\b", first.lower()))


def is_ambiguous(user_text: str) -> bool:
    t = (user_text or "").strip().lower()
    if not t:
        return False

    pronouny = bool(re.search(r"\b(it|that|this|they|them)\b", t))
    has_target_keywords = any(k in t for k in ["tts", "voice", "pitch", "speed", "warmup", "log", "command", "module"])

    if pronouny and not has_target_keywords:
        return True

    if re.search(r"\b(make|change|fix|update)\s+it\b", t) and not has_target_keywords:
        return True

    return False


def maybe_followup(user_text: str, style: Style) -> str:
    if not is_ambiguous(user_text):
        return ""
    if _rng().random() > FOLLOWUP_RATE:
        return ""
    return "Quick check: what part should I change—voice, logging, or commands?"


def apply_human_layer(
    reply: str,
    user_text: str,
    style: Style,
    thought_trace: bool = THOUGHT_TRACE_DEFAULT,
    # Back-compat alias: some call-sites may pass trace=True
    trace: Optional[bool] = None,
    thought_logger: Callable[[str], None] | None = None,
) -> str:
    """Apply the humanization layer.

    If thought_trace=True, prints a compact JSON "thought pattern" describing the
    internal signals + decisions this module actually used (no raw user/reply text).
    """
    if trace is not None:
        thought_trace = trace

    out = (reply or "").strip()
    if not out:
        if thought_trace:
            emit_thought(
                {"module": "humanize", "event": "skip", "reason": "empty_reply"},
                thought_logger,
            )
        return out

    # --- signals the layer actually uses ---
    intent = infer_intent(user_text)
    constraints = extract_constraints(user_text)
    ambiguous = is_ambiguous(user_text)

    listen_eligible = should_listen(user_text)
    reply_already_listening = looks_like_it_already_listened(out)

    # --- apply transforms ---
    parts: list[str] = []
    used_listening_line = False
    added_followup = False

    if listen_eligible and not reply_already_listening:
        parts.append(strip_listening_label(listening_line(user_text, style)))
        used_listening_line = True

    parts.append(out)

    if "?" not in out:
        fu = maybe_followup(user_text, style)
        if fu:
            parts.append(fu)
            added_followup = True

    final = "\n".join(parts).strip()

    if thought_trace:
        style_bucket = "relaxed" if style.relax >= 0.60 else ("neutral" if style.relax >= 0.35 else "strict")
        energy_bucket = "high" if style.arousal > 0.30 else ("low" if style.arousal < -0.15 else "mid")

        emit_thought(
            {
                "module": "humanize",
                "event": "thought_pattern",
                "constants": {
                    "DETERMINISTIC": bool(DETERMINISTIC),
                    "LISTENING_RATE": LISTENING_RATE,
                    "FOLLOWUP_RATE": FOLLOWUP_RATE,
                },
                "signals": {
                    "intent": intent,
                    "constraints_count": len(constraints),
                    "ambiguous": ambiguous,
                    "style_bucket": style_bucket,
                    "energy_bucket": energy_bucket,
                },
                "decisions": {
                    "listen_eligible": listen_eligible,
                    "reply_already_listening": reply_already_listening,
                    "used_listening_line": used_listening_line,
                    "added_followup": added_followup,
                },
            },
            thought_logger,
        )

    return final
