# humanize.py
from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Optional

# If you want deterministic behavior while testing, set to True
DETERMINISTIC = False

# How often to prepend a listening line (0..1)
LISTENING_RATE = 0.85

# Max one follow-up question, and only when ambiguity is detected
FOLLOWUP_RATE = 0.45


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
        emoji = "Allowed occasionally (max 1 per reply)."
        humor = "Light humor is OK if the user’s tone invites it."
    elif style.relax >= 0.35:
        emoji = "Avoid emojis unless the user uses them first."
        humor = "Keep it mostly straightforward; tiny playfulness is OK."
    else:
        emoji = "No emojis."
        humor = "Stay professional and neutral."

    return (
        "Conversation style rules:\n"
        "- Start most replies with a short 'listening' line (1 sentence): acknowledge intent + any constraints, then answer.\n"
        "- Do NOT repeat the user’s message verbatim.\n"
        "- Vary response length: one-liners are fine; use bullets only when helpful.\n"
        "- Ask at most ONE follow-up question, and only if it changes the solution.\n"
        "- Use natural contractions (don't, you'll, that's) and avoid overly formal phrasing.\n"
        f"- Emojis: {emoji}\n"
        f"- Humor: {humor}\n"
    ).strip()


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


def apply_human_layer(reply: str, user_text: str, style: Style) -> str:
    out = (reply or "").strip()
    if not out:
        return out

    parts: list[str] = []

    if should_listen(user_text) and not looks_like_it_already_listened(out):
        parts.append(listening_line(user_text, style))

    parts.append(out)

    if "?" not in out:
        fu = maybe_followup(user_text, style)
        if fu:
            parts.append(fu)

    return "\n".join(parts).strip()
