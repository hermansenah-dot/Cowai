import discord
import time
import json
import re
import pytz
from datetime import datetime, timedelta

# ---- Internal modules ----
from ai import ask_llama
from triggers import analyze_input
from emotion import emotion
from personality.memory_short import get_short_memory
from personality.memory_long import Long_Term_Memory
from reminders import ReminderStore, reminder_loop, Reminder
from config import DISCORD_TOKEN, ALLOWED_CHANNEL_IDS
from tts_coqui import handle_tts_command,  warmup_tts


def looks_english(text: str) -> bool:
    """
    Heuristic check for English text.
    Returns False for messages that look non-English.
    """

    # Too short → ignore
    if len(text) < 2:
        return False

    # If it has a lot of non-latin letters, ignore
    non_latin = re.findall(r"[^\x00-\x7F]", text)
    if len(non_latin) > 2:
        return False

    # Common English words check
    english_hits = sum(
        1 for w in ["the", "be", "to", "of", "and",
    "a", "in", "that", "have", "i",
    "it", "for", "not", "on", "with",
    "he", "as", "you", "do", "at",]
        if re.search(rf"\b{w}\b", text.lower())
    )

    return english_hits >= 1

# =========================
# Reminder system
# =========================

# Persistent reminder store (survives restarts)
store = ReminderStore()

# Default timezone
DEFAULT_TZ = pytz.timezone("Europe/Copenhagen")


def parse_in_minutes(text: str):
    """
    Rule-based parser for:
      "remind me in 10 minutes drink water"
    Returns:
      int minutes or None
    """
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


def parse_at_time(text: str):
    """
    Rule-based absolute time parser for common patterns.
    Supports:
      - "remind me at 18:30 to call mom"
      - "at 6pm remind me to stand up"
      - "remind me tomorrow at 07:15 check email"
    Returns:
      dict { "hour": int, "minute": int, "day_offset": 0|1, "text": str } or None
    """
    t = text.lower().strip()

    # Require "remind" somewhere so we don't treat random chat as a reminder
    if "remind" not in t:
        return None

    day_offset = 1 if "tomorrow" in t else 0

    # 24h format: HH:MM
    m = re.search(r"\b(at\s*)?([01]?\d|2[0-3]):([0-5]\d)\b", t)
    hour = minute = None

    if m:
        hour = int(m.group(2))
        minute = int(m.group(3))
    else:
        # 12h format: H(am/pm) or H:MM(am/pm)
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

    # Extract reminder text: take everything after "remind me" if possible, else after time
    reminder_text = None

    if "remind me" in t:
        reminder_text = text.lower().split("remind me", 1)[1].strip()
    else:
        reminder_text = text.strip()

    # Clean the reminder text a bit (remove obvious timing phrases)
    # This is intentionally simple; LLM extraction below will do better.
    for token in ["tomorrow", "at", "am", "pm"]:
        reminder_text = reminder_text.replace(token, " ")

    reminder_text = " ".join(reminder_text.split()).strip(" .!-")
    if not reminder_text:
        reminder_text = "You asked me to remind you."

    return {"hour": hour, "minute": minute, "day_offset": day_offset, "text": reminder_text}


def build_due_ts_absolute(hour: int, minute: int, day_offset: int, tz=DEFAULT_TZ) -> float:
    """
    Convert an absolute (hour, minute) to a unix timestamp using pytz.
    """
    now = datetime.now(tz)

    target = now.replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0
    )

    if day_offset == 1:
        target = target + timedelta(days=1)

    if day_offset == 0 and target <= now:
        target = target + timedelta(days=1)

    return target.timestamp()


def llm_extract_reminder(user_text: str):
    """
    Natural language reminder extraction (safe).
    The LLM outputs strict JSON ONLY. Python validates and schedules.

    Supported:
      - Relative: "in 10 minutes", "in an hour", "half an hour"
      - Absolute: "at 18:30", "tomorrow at 7", "at 6pm"
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
                "- If time_type='relative': set delay_minutes (e.g. 10). hour/minute/day_offset must be null.\n"
                "- If time_type='absolute': set hour (0-23), minute (0-59), day_offset (0=today, 1=tomorrow). delay_minutes must be null.\n"
                "- Interpret common durations: 'half an hour'=30, 'an hour'=60, 'a couple minutes'=2, 'a few minutes'=5.\n"
                "- If the user says 'tomorrow', set day_offset=1.\n"
                "- text must be the reminder content (short). Remove timing words.\n"
                "- If you cannot extract safely, return intent='none'.\n"
            ),
        },
        {"role": "user", "content": user_text},
    ]

    # Use your existing LLM call; keep it deterministic-ish
    reply = ask_llama(extraction_messages)

    try:
        data = json.loads(reply)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None
    if data.get("intent") != "set_reminder":
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

    print(f"Logged in as {client.user} (ID: {client.user.id})")

    # Start reminders loop (your existing stuff)
    client.loop.create_task(reminder_loop(client, store))

    # Pre-warm Coqui TTS so first !tts is instant
    client.loop.create_task(warmup_tts())

    print("[TTS] Warmup complete.")



@client.event
async def on_message(message):
    """
    Main message handler.
    Order matters:
    1) Ignore self
    2) Handle reminders
    3) Handle AI conversation
    """

    # Ignore messages from the bot itself
    if message.author == client.user:
        return

    content = message.content.strip()

    if content.lower().startswith("!tts"):
        pass
        text = content[4:].strip()
        if not text:
            await message.channel.send("Usage: `!tts your text here`")
            return

    await handle_tts_command(message, text)
    return

    # 👇 channel restriction
    if message.channel.id not in ALLOWED_CHANNEL_IDS:
        return

    # Ignore non-English messages
    # if not looks_english(content):
    #    return

    # =========================
    # Reminder handling
    # =========================

    # 1) Fast path: your simple "in X minutes" parser
    mins = parse_in_minutes(content)
    reminder_text = None
    due_ts = None

    if mins is not None:
        reminder_text = content.split("minutes", 1)[-1].strip() or "You asked me to remind you."
        due_ts = time.time() + mins * 60

    # 2) Fast-ish absolute parser (rule-based)
    if due_ts is None:
        abs_parsed = parse_at_time(content)
        if abs_parsed:
            reminder_text = abs_parsed["text"]
            due_ts = build_due_ts_absolute(abs_parsed["hour"], abs_parsed["minute"], abs_parsed["day_offset"])

    # 3) Natural language fallback via LLM (relative or absolute)
    if due_ts is None:
        extracted = llm_extract_reminder(content)
        if extracted:
            reminder_text = extracted["text"]
            if extracted["type"] == "relative":
                mins = extracted["delay_minutes"]
                due_ts = time.time() + mins * 60
            else:
                due_ts = build_due_ts_absolute(extracted["hour"], extracted["minute"], extracted["day_offset"])

    # If we managed to schedule anything, do it and confirm
    if due_ts is not None and reminder_text is not None:
        store.add(Reminder(
            due_ts=due_ts,
            channel_id=message.channel.id,
            user_id=message.author.id,
            text=reminder_text
        ))

        # 24h confirmation time in server timezone (DEFAULT_TZ)
        time_str = datetime.fromtimestamp(due_ts, DEFAULT_TZ).strftime("%H:%M")

        await message.channel.send(
            f"⏰ Got it.\n"
            f"I’ll remind you at **{time_str}**.\n"
            f"Message: *{reminder_text}* 😊"
        )
        return  # Do NOT continue to AI logic

    # =========================
    # AI conversation handling
    # =========================

    # Strip bot mention if present
    user_text = content.replace(f"<@{client.user.id}>", "").strip()
    if not user_text:
        return

    username = message.author.display_name
    user_id = message.author.id

    print(f"USER {username}: {user_text}")

    try:
        # Get per-user memory
        short_memory = get_short_memory(user_id)
        long_memory = Long_Term_Memory(user_id)

        # Emotion processing
        delta = analyze_input(user_text)
        emotion.apply(delta)

        # Update long-term memory
        long_memory.update_from_text(user_text)

        # Refresh system prompt (persona + emotion + time)
        short_memory.refresh_system()

        # Inject long-term facts as system info
        short_memory.messages[0]["content"] += (
            "\n\nKnown facts:\n" + long_memory.as_prompt()
        )

        # Add user message
        short_memory.add("user", user_text)

        # Build chat messages for Ollama
        messages = short_memory.get_messages()

        # Ask LLM
        reply = ask_llama(messages)

        # Store assistant reply
        short_memory.add("assistant", reply)

        # Emotion decay over time
        emotion.decay()

        print(f"AI response to {username}: {reply}")
        await message.channel.send(reply)

        print(f"MOOD {emotion.to_int()} ({emotion.label()}) | USER {username}: {user_text}")

    except Exception as e:
        print("Bot error:", e)
        await message.channel.send("There is an issue with my AI.")


# =========================
# Start bot
# =========================

client.run(DISCORD_TOKEN)