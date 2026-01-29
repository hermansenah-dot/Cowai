"""bot.py

Discord bot entrypoint.

Features:
- Channel allow-list (ALLOWED_CHANNEL_IDS in config.py)
- Optional English-only filter (looks_english)
- Reminders (relative + absolute + LLM extraction fallback)
- Coqui TTS command: !tts <text> (joins VC, speaks, leaves)
- Voice toggle: !voice on/off (auto TTS for AI replies when enabled)
- LLM chat with short/long memory + emotion drift

Notes:
- Keep command handlers self-contained and `return` after handling to avoid
  variable-scope bugs (e.g. UnboundLocalError on `text`).
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta

import discord
import pytz

# ---- Internal modules ----
from ai import ask_llama
from triggers import analyze_input
from emotion import emotion
from personality.memory_short import get_short_memory

# Your long-memory class name has changed in the past; support both.
try:
    from personality.memory_long import LongTermMemory as LongMemory
except Exception:
    # fallback for older naming
    from personality.memory_long import Long_Term_Memory as LongMemory  # type: ignore

from reminders import ReminderStore, reminder_loop, Reminder
from config import DISCORD_TOKEN, ALLOWED_CHANNEL_IDS
from tts_coqui import handle_tts_command, warmup_tts


# =========================
# Voice toggle state
# =========================
# Cache per-user setting in memory. Persisted to LongMemory as "voice_enabled".
VOICE_ENABLED: dict[int, bool] = {}


def get_voice_enabled(user_id: int) -> bool:
    """Get voice-enabled setting for a user (cached, persisted in LongMemory)."""
    if user_id in VOICE_ENABLED:
        return VOICE_ENABLED[user_id]

    try:
        lm = LongMemory(user_id)
        enabled = bool(getattr(lm, "data", {}).get("voice_enabled", False))
    except Exception:
        enabled = False

    VOICE_ENABLED[user_id] = enabled
    return enabled


def set_voice_enabled(user_id: int, enabled: bool) -> None:
    """Set voice-enabled setting for a user (cache + persist)."""
    VOICE_ENABLED[user_id] = bool(enabled)

    try:
        lm = LongMemory(user_id)
        if hasattr(lm, "data"):
            lm.data["voice_enabled"] = bool(enabled)  # type: ignore[attr-defined]
            lm.save()
    except Exception as e:
        print("[Voice] Failed to persist voice_enabled:", e)


def truncate_for_tts(text: str, max_chars: int = 220) -> str:
    """Keep auto-TTS short so it doesn't drone."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "..."


# =========================
# Language filter
# =========================

COMMON_ENGLISH_WORDS = [
    "the", "be", "to", "of", "and",
    "a", "in", "that", "have", "i",
    "it", "for", "not", "on", "with",
    "he", "as", "you", "do", "at",
]


def looks_english(text: str) -> bool:
    """Heuristic English detector. Returns False for likely non-English."""
    text = text.strip()
    if len(text) < 2:
        return False

    # If it has a lot of non-ASCII characters, treat it as non-English.
    non_ascii = re.findall(r"[^\x00-\x7F]", text)
    if len(non_ascii) > 2:
        return False

    # Count hits on common English words.
    lower = text.lower()
    hits = sum(1 for w in COMMON_ENGLISH_WORDS if re.search(rf"\b{w}\b", lower))
    return hits >= 1


# =========================
# Reminder system
# =========================

store = ReminderStore()
DEFAULT_TZ = pytz.timezone("Europe/Copenhagen")


def parse_in_minutes(text: str) -> int | None:
    """Parse: 'remind me in 10 minutes <text>' -> minutes"""
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


def parse_at_time(text: str) -> dict | None:
    """
    Parse common absolute-time patterns:
      - 'remind me at 18:30 to call mom'
      - 'at 6pm remind me to stand up'
      - 'remind me tomorrow at 07:15 check email'

    Returns:
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

    # crude text extraction
    reminder_text = text
    if "remind me" in t:
        reminder_text = text.lower().split("remind me", 1)[1].strip()

    for token in ["tomorrow", "at", "am", "pm"]:
        reminder_text = reminder_text.replace(token, " ")

    reminder_text = " ".join(reminder_text.split()).strip(" .!-")
    if not reminder_text:
        reminder_text = "You asked me to remind you."

    return {"hour": hour, "minute": minute, "day_offset": day_offset, "text": reminder_text}


def build_due_ts_absolute(hour: int, minute: int, day_offset: int, tz=DEFAULT_TZ) -> float:
    """Convert (hour, minute) + day_offset to a unix timestamp."""
    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if day_offset == 1:
        target = target + timedelta(days=1)

    # If time already passed today, schedule for tomorrow
    if day_offset == 0 and target <= now:
        target = target + timedelta(days=1)

    return target.timestamp()


def llm_extract_reminder(user_text: str) -> dict | None:
    """
    Natural language reminder extraction (safe).
    The LLM outputs strict JSON ONLY. Python validates & schedules.
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


# =========================
# Discord setup
# =========================

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    """Runs once when the bot connects."""
    print(f"Logged in as {client.user} (ID: {client.user.id})")

    # Start background reminder scheduler
    client.loop.create_task(reminder_loop(client, store))

    # Pre-warm Coqui TTS so the first !tts is fast
    client.loop.create_task(warmup_tts())


@client.event
async def on_message(message: discord.Message):
    """Main message handler."""

    # Ignore messages from the bot itself
    if message.author == client.user:
        return

    content = message.content.strip()
    if not content:
        return

    # =========================
    # Commands (handle + return)
    # =========================

    # !tts <text>  (joins your voice channel, speaks, leaves)
    if content.lower().startswith("!tts"):
        text = content[4:].strip()
        if not text:
            await message.channel.send("Usage: `!tts your text here`")
            return

        await handle_tts_command(message, text)
        return

    # =========================
    # Channel restriction
    # =========================
    if message.channel.id not in ALLOWED_CHANNEL_IDS:
        return

    # !voice on/off/status (auto-speak AI replies)
    if content.lower().startswith("!voice"):
        arg = content[6:].strip().lower()
        uid = message.author.id

        if arg in {"on", "enable", "true", "1"}:
            set_voice_enabled(uid, True)
            await message.channel.send("🔊 Voice replies: **ON**")
            return

        if arg in {"off", "disable", "false", "0"}:
            set_voice_enabled(uid, False)
            await message.channel.send("🔇 Voice replies: **OFF**")
            return

        state = get_voice_enabled(uid)
        await message.channel.send(
            f"Voice replies are currently: **{'ON' if state else 'OFF'}**\n"
            "Use `!voice on` or `!voice off`."
        )
        return

    # =========================
    # English-only filter (optional)
    # =========================
    # if not looks_english(content):
    #     await message.channel.send("English only, please. 💜")
    #     return

    # =========================
    # Reminder handling
    # =========================

    mins = parse_in_minutes(content)
    reminder_text = None
    due_ts = None

    # Relative: "remind me in X minutes ..."
    if mins is not None:
        reminder_text = content.split("minutes", 1)[-1].strip() or "You asked me to remind you."
        due_ts = time.time() + mins * 60

    # Absolute: "at 18:30 ..." / "tomorrow at 7pm ..."
    if due_ts is None:
        abs_parsed = parse_at_time(content)
        if abs_parsed:
            reminder_text = abs_parsed["text"]
            due_ts = build_due_ts_absolute(abs_parsed["hour"], abs_parsed["minute"], abs_parsed["day_offset"])

    # Natural language (LLM) fallback
    if due_ts is None:
        extracted = llm_extract_reminder(content)
        if extracted:
            reminder_text = extracted["text"]
            if extracted["type"] == "relative":
                mins = extracted["delay_minutes"]
                due_ts = time.time() + mins * 60
            else:
                due_ts = build_due_ts_absolute(extracted["hour"], extracted["minute"], extracted["day_offset"])

    if due_ts is not None and reminder_text is not None:
        store.add(
            Reminder(
                due_ts=due_ts,
                channel_id=message.channel.id,
                user_id=message.author.id,
                text=reminder_text,
            )
        )

        time_str = datetime.fromtimestamp(due_ts, DEFAULT_TZ).strftime("%H:%M")
        await message.channel.send(
            f"⏰ Got it.\n"
            f"I’ll remind you at **{time_str}**.\n"
            f"Message: *{reminder_text}* 😊"
        )
        return

    # =========================
    # AI conversation handling
    # =========================

    user_text = content.replace(f"<@{client.user.id}>", "").strip()
    if not user_text:
        return

    username = message.author.display_name
    user_id = message.author.id
    print(f"USER {username}: {user_text}")

    try:
        short_memory = get_short_memory(user_id)
        long_memory = LongMemory(user_id)

        # Emotion drift
        delta = analyze_input(user_text)
        emotion.apply(delta)

        # Long-term memory update
        long_memory.update_from_text(user_text)

        # Refresh system prompt (persona + emotion + time)
        short_memory.refresh_system()

        # Inject long-term facts into system content
        short_memory.messages[0]["content"] += "\n\nKnown facts:\n" + long_memory.as_prompt()

        # Add user message
        short_memory.add("user", user_text)

        # Ask LLM
        reply = ask_llama(short_memory.get_messages())

        # Store assistant reply
        short_memory.add("assistant", reply)

        # Decay emotion
        emotion.decay()

        print(f"AI -> {username}: {reply}")
        await message.channel.send(reply)

        # Auto-speak in VC if user enabled voice
        if get_voice_enabled(user_id):
            if message.author.voice and message.author.voice.channel:
                spoken = truncate_for_tts(reply, max_chars=220)
                try:
                    await handle_tts_command(message, spoken)
                except Exception as e:
                    print("[TTS] auto-voice failed:", e)

    except Exception as e:
        print("Bot error:", e)
        await message.channel.send("There is an issue with my AI.")


# =========================
# Start bot
# =========================

client.run(DISCORD_TOKEN)
