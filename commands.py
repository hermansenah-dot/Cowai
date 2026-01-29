"""commands.py

All Discord command handling lives here.

Important design rule (per your requirement):
- bot.py must enforce ALLOWED_CHANNEL_IDS BEFORE calling any command handler
- therefore, every command response happens only in allowed channels

Commands in this file:
- !tts <text>          -> join your VC, speak, leave
- !voice on/off/status -> auto-speak AI replies (per user, persisted)
- !reminder <text>     -> reminders ONLY when prefixed with !reminder
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import discord

from tts_coqui import handle_tts_command, handle_tts_lines
from reminders import Reminder, ReminderStore
from ai import ask_llama


# -------------------------
# Voice toggle (per-user)
# -------------------------

VOICE_ENABLED: Dict[int, bool] = {}


def get_voice_enabled(user_id: int, LongMemory) -> bool:
    """Cached + persisted per-user voice toggle."""
    if user_id in VOICE_ENABLED:
        return VOICE_ENABLED[user_id]

    try:
        lm = LongMemory(user_id)
        enabled = bool(getattr(lm, "data", {}).get("voice_enabled", False))
    except Exception:
        enabled = False

    VOICE_ENABLED[user_id] = enabled
    return enabled


def set_voice_enabled(user_id: int, enabled: bool, LongMemory) -> None:
    """Persist voice toggle in long memory so it survives restarts."""
    VOICE_ENABLED[user_id] = bool(enabled)
    try:
        lm = LongMemory(user_id)
        if hasattr(lm, "data"):
            lm.data["voice_enabled"] = bool(enabled)  # type: ignore[attr-defined]
            lm.save()
    except Exception as e:
        print("[Voice] Failed to persist voice_enabled:", e)


def truncate_for_tts(text: str, max_chars: int = 600) -> str:
    """Keep auto-TTS short so it doesn't drone in VC."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "..."


def chunk_text_for_tts(text: str, max_chars: int = 260, max_parts: int = 6) -> list[str]:
    """Split text into short-ish chunks for TTS (sentence-aware)."""
    text = (text or "").strip()
    if not text:
        return []

    text = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"(?<=[.!?])\s+", text)

    chunks: list[str] = []
    buf = ""

    def flush():
        nonlocal buf
        if buf.strip():
            chunks.append(buf.strip())
        buf = ""

    for s in sentences:
        s = s.strip()
        if not s:
            continue

        if len(buf) + len(s) + 1 <= max_chars:
            buf = (buf + " " + s).strip()
        else:
            flush()
            if len(s) <= max_chars:
                buf = s
            else:
                start = 0
                while start < len(s) and len(chunks) < max_parts:
                    chunks.append(s[start:start + max_chars].strip())
                    start += max_chars
                buf = ""

        if len(chunks) >= max_parts:
            break

    flush()

    if len(chunks) > max_parts:
        chunks = chunks[:max_parts]

    if chunks and len(text) > sum(len(c) for c in chunks) + (len(chunks) - 1):
        chunks[-1] = chunks[-1].rstrip() + " …"

    return chunks


async def maybe_auto_voice_reply(message: discord.Message, reply: str, LongMemory) -> None:
    """
    Speak AI reply in VC if the author has !voice on and is currently in a voice channel.
    Safe to call after sending the text reply.
    """
    if not get_voice_enabled(message.author.id, LongMemory):
        return
    if not (message.author.voice and message.author.voice.channel):
        return

    lines = chunk_text_for_tts(reply, max_chars=260, max_parts=6)
    try:
        await handle_tts_lines(message, lines)

    except Exception as e:
        print("[TTS] auto-voice failed:", e)


# -------------------------
# Reminder parsing helpers
# -------------------------

def parse_in_minutes(text: str) -> Optional[int]:
    """Parse: 'remind me in 10 minutes <text>' -> minutes (int)"""
    t = text.lower()
    if "remind me in" not in t:
        return None

    after = t.split("remind me in", 1)[1].strip()
    parts = after.split()
    if not parts:
        return None

    try:
        mins = int(parts[0])
    except ValueError:
        return None

    if "minute" not in after:
        return None

    return mins


def parse_at_time(text: str) -> Optional[Dict[str, Any]]:
    """
    Parse common absolute-time patterns:
      - 'remind me at 18:30 to call mom'
      - 'at 6pm remind me to stand up'
      - 'remind me tomorrow at 07:15 check email'

    Returns dict:
      {hour:int, minute:int, day_offset:int, text:str} or None
    """
    t = text.lower().strip()
    if "remind" not in t:
        return None

    day_offset = 1 if "tomorrow" in t else 0

    # 24h format HH:MM
    m = re.search(r"\b(at\s*)?([01]?\d|2[0-3]):([0-5]\d)\b", t)
    hour = minute = None

    if m:
        hour = int(m.group(2))
        minute = int(m.group(3))
    else:
        # 12h format H(am/pm) or H:MM(am/pm)
        m2 = re.search(r"\b(at\s*)?([1-9]|1[0-2])(?::([0-5]\d))?\s*(am|pm)\b", t)
        if not m2:
            return None

        h12 = int(m2.group(2))
        minute = int(m2.group(3)) if m2.group(3) else 0
        ampm = m2.group(4)

        if ampm == "am":
            hour = 0 if h12 == 12 else h12
        else:
            hour = 12 if h12 == 12 else h12 + 12

    # crude reminder text extraction
    reminder_text = text
    if "remind me" in t:
        reminder_text = text.lower().split("remind me", 1)[1].strip()

    for token in ["tomorrow", "at", "am", "pm"]:
        reminder_text = reminder_text.replace(token, " ")

    reminder_text = " ".join(reminder_text.split()).strip(" .!-")
    if not reminder_text:
        reminder_text = "You asked me to remind you."

    return {"hour": hour, "minute": minute, "day_offset": day_offset, "text": reminder_text}


def build_due_ts_absolute(hour: int, minute: int, day_offset: int, tz) -> float:
    """Convert (hour, minute) + day_offset to a Unix timestamp."""
    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if day_offset == 1:
        target = target + timedelta(days=1)

    # If time already passed today, schedule for tomorrow
    if day_offset == 0 and target <= now:
        target = target + timedelta(days=1)

    return target.timestamp()


def llm_extract_reminder(user_text: str) -> Optional[Dict[str, Any]]:
    """
    Natural language reminder extraction.
    Only used behind the explicit !reminder command.
    The LLM must output strict JSON.
    """
    extraction_messages = [
        {
            "role": "system",
            "content": (
                "You are a strict JSON extraction tool. Output JSON ONLY.\n"
                "Detect whether the user wants to set a reminder.\n\n"
                "Schema:\n"
                "{\n"
                '  "intent": "set_reminder" | "none",\n'
                '  "time_type": "relative" | "absolute" | null,\n'
                '  "delay_minutes": integer | null,\n'
                '  "hour": integer | null,\n'
                '  "minute": integer | null,\n'
                '  "day_offset": integer | null,\n'
                '  "text": string | null\n'
                "}\n\n"
                "Rules:\n"
                "- If NOT a reminder request: intent='none'.\n"
                "- If time_type='relative': set delay_minutes. hour/minute/day_offset must be null.\n"
                "- If time_type='absolute': set hour (0-23), minute (0-59), day_offset (0=today, 1=tomorrow). delay_minutes must be null.\n"
                "- Interpret: 'half an hour'=30, 'an hour'=60, 'a couple minutes'=2, 'a few minutes'=5.\n"
                "- If user says 'tomorrow', day_offset=1.\n"
                "- text must be reminder content (short). Remove timing words.\n"
                "- If you cannot extract safely, return intent='none'.\n"
            ),
        },
        {"role": "user", "content": user_text},
    ]

    reply = ask_llama(extraction_messages)

    try:
        data = json.loads(reply)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict) or data.get("intent") != "set_reminder":
        return None

    time_type = data.get("time_type")
    text = data.get("text")

    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()

    if time_type == "relative":
        delay = data.get("delay_minutes")
        if not isinstance(delay, int) or delay <= 0 or delay > 24 * 60:
            return None
        return {"type": "relative", "delay_minutes": delay, "text": text}

    if time_type == "absolute":
        hour = data.get("hour")
        minute = data.get("minute")
        day_offset = data.get("day_offset", 0)

        if not isinstance(hour, int) or not (0 <= hour <= 23):
            return None
        if not isinstance(minute, int) or not (0 <= minute <= 59):
            return None
        if not isinstance(day_offset, int) or day_offset not in (0, 1):
            return None

        return {"type": "absolute", "hour": hour, "minute": minute, "day_offset": day_offset, "text": text}

    return None


# -------------------------
# Command router
# -------------------------

async def handle_commands(
    message: discord.Message,
    content: str,
    *,
    store: ReminderStore,
    default_tz,
    LongMemory,
) -> bool:
    """
    Central command router.
    Returns True if a command was handled (caller should return).
    """

    # ---- !tts ----
    if content.lower().startswith("!tts"):
        text = content[4:].strip()
        if not text:
            await message.channel.send("Usage: `!tts your text here`")
            return True

        await handle_tts_command(message, text)
        return True

    # ---- !voice ----
    if content.lower().startswith("!voice"):
        arg = content[6:].strip().lower()
        uid = message.author.id

        if arg in {"on", "enable", "true", "1"}:
            set_voice_enabled(uid, True, LongMemory)
            await message.channel.send("🔊 Voice replies: **ON**")
            return True

        if arg in {"off", "disable", "false", "0"}:
            set_voice_enabled(uid, False, LongMemory)
            await message.channel.send("🔇 Voice replies: **OFF**")
            return True

        state = get_voice_enabled(uid, LongMemory)
        await message.channel.send(
            f"Voice replies are currently: **{'ON' if state else 'OFF'}**\n"
            "Use `!voice on` or `!voice off`."
        )
        return True

    # ---- !reminder ----
    if content.lower().startswith("!reminder"):
        reminder_input = content[len("!reminder"):].strip()

        if not reminder_input:
            await message.channel.send(
                "Usage:\n"
                "`!reminder remind me in 10 minutes drink water`\n"
                "`!reminder remind me tomorrow at 18:30 dinner`"
            )
            return True

        mins = parse_in_minutes(reminder_input)
        reminder_text = None
        due_ts = None

        # Relative: "remind me in X minutes ..."
        if mins is not None:
            reminder_text = reminder_input.split("minutes", 1)[-1].strip() or "You asked me to remind you."
            due_ts = time.time() + mins * 60

        # Absolute: "remind me at 18:30 ..." / "tomorrow at 7pm ..."
        if due_ts is None:
            abs_parsed = parse_at_time(reminder_input)
            if abs_parsed:
                reminder_text = abs_parsed["text"]
                due_ts = build_due_ts_absolute(
                    abs_parsed["hour"],
                    abs_parsed["minute"],
                    abs_parsed["day_offset"],
                    default_tz,
                )

        # LLM fallback — ONLY because user explicitly typed !reminder
        if due_ts is None:
            extracted = llm_extract_reminder(reminder_input)
            if extracted:
                reminder_text = extracted["text"]
                if extracted["type"] == "relative":
                    due_ts = time.time() + extracted["delay_minutes"] * 60
                else:
                    due_ts = build_due_ts_absolute(
                        extracted["hour"],
                        extracted["minute"],
                        extracted["day_offset"],
                        default_tz,
                    )

        if due_ts is None or reminder_text is None:
            await message.channel.send(
                "I couldn’t understand that reminder.\n"
                "Try:\n"
                "`!reminder remind me in 10 minutes stand up`\n"
                "`!reminder remind me tomorrow at 8pm check email`"
            )
            return True

        store.add(
            Reminder(
                due_ts=due_ts,
                channel_id=message.channel.id,
                user_id=message.author.id,
                text=reminder_text,
            )
        )

        time_str = datetime.fromtimestamp(due_ts, default_tz).strftime("%H:%M")
        await message.channel.send(
            f"⏰ Got it.\n"
            f"I’ll remind you at **{time_str}**.\n"
            f"Message: *{reminder_text}* 😊"
        )
        return True

    return False
