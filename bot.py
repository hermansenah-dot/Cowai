"""
Discord AI bot (chat + commands)

Key rules:
- The bot only responds in ALLOWED_CHANNEL_IDS.
- Reminders are created ONLY via the explicit `!reminder ...` command.
- Commands live in commands.py (single source of truth).

Notes:
- ask_llama() is synchronous, so we run it in a thread to avoid blocking Discord's event loop.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from datetime import datetime
from typing import Optional

import discord
import pytz

from ai import ask_llama
from config import DISCORD_TOKEN, ALLOWED_CHANNEL_IDS
from emotion import emotion
from personality.memory_long import Long_Term_Memory
from personality.memory_short import get_short_memory
from reminders import ReminderStore, reminder_loop
from triggers import analyze_input

# Centralized command router
from commands import handle_commands, get_voice_enabled, maybe_speak_reply
# =========================
# Configuration
# =========================

# Discord hard-limit is 2000 chars; we keep replies shorter for readability.
DISCORD_MAX = 2000

# Only respond in these channels (hard gate).
# Defined in config.py as ALLOWED_CHANNEL_IDS.

# Default timezone for reminders / timestamps.
DEFAULT_TZ = pytz.timezone("Europe/Copenhagen")

# Resolve file paths relative to this script (so running from another working directory still works)
BASE_DIR = Path(__file__).resolve().parent

# Path to the banned-words list (one lowercase word per line).
# Lines starting with "#" are comments.
BANNED_WORDS_FILE = str(BASE_DIR / "banned_words.txt")


# Where we log censorship events (appends one line per censored AI reply).
CENSOR_LOG_FILE = str(BASE_DIR / "logs" / "filtered_words.txt")


def log_censorship(filtered_counts: dict[str, int]) -> None:
    """
    Append a censorship event to CENSOR_LOG_FILE.

    Format:
      [YYYY-MM-DD HH:MM:SS] filtered: word1(x2), word2(x1)
    """
    if not filtered_counts:
        return

    ts = datetime.now(DEFAULT_TZ).strftime("%Y-%m-%d %H:%M:%S")
    items = ", ".join(f"{w}(x{n})" for w, n in sorted(filtered_counts.items()))
    log_line = f"[{ts}] filtered: {items}\n"

    try:
        # Ensure the logs folder exists
        Path(CENSOR_LOG_FILE).parent.mkdir(parents=True, exist_ok=True)

        with open(CENSOR_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_line)
    except Exception as e:
        # Logging should never break the bot, but we do want visibility.
        try:
            print(f"[{ts}] censor log write failed: {e} | path={CENSOR_LOG_FILE}")
        except Exception:
            pass

def load_banned_words(path: str = BANNED_WORDS_FILE) -> set[str]:
    """Load banned words from a text file (one word per line)."""
    words: set[str] = set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                words.add(line.lower())
    except FileNotFoundError:
        # If the file doesn't exist, just keep the filter disabled.
        pass
    return words


BANNED_WORDS: set[str] = load_banned_words()


def filter_banned_words(text: str) -> str:
    """
    Censor banned words in AI replies.

    - Whole-word match (case-insensitive).
    - Replaces the matched word with asterisks of the same length.
    """
    if not text or not BANNED_WORDS:
        return text

    filtered_counts: dict[str, int] = {}

    out = text
    for w in sorted(BANNED_WORDS, key=len, reverse=True):
        # Whole-word boundary to avoid censoring parts of other words.
        pattern = re.compile(rf"\b{re.escape(w)}\b", re.IGNORECASE)
        out, n = pattern.subn("*FILTERED!*", out)
        if n:
            filtered_counts[w] = filtered_counts.get(w, 0) + int(n)

    if filtered_counts:
        log_censorship(filtered_counts)

    return out



def log(message: str) -> None:
    """Print console messages with a local timestamp (Europe/Copenhagen)."""
    ts = datetime.now(DEFAULT_TZ).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {message}")


# Prevent spamming “ready” messages on reconnects.
READY_ANNOUNCED = False


# =========================
# Utilities: message splitting
# =========================

def split_for_discord(
    text: str,
    *,
    max_len: int = 750,
    max_parts: int = 5,
    max_sentences_per_chunk: int = 5,
) -> list[str]:
    """
    Split a reply into multiple Discord messages.

    Strategy:
    - Prefer sentence boundaries.
    - Aim for <= max_sentences_per_chunk sentences per message.
    - Respect max_len (Discord has a hard 2000 char limit).
    - Cap at max_parts to avoid spam.
    """
    text = (text or "").strip()
    if not text:
        return []

    # Normalize whitespace/newlines
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    # Sentence-ish splitting (keeps punctuation)
    sentences = re.split(r"(?<=[.!?])\s+", text)

    chunks: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        if buf:
            chunks.append(" ".join(buf).strip())
        buf = []

    for s in sentences:
        s = s.strip()
        if not s:
            continue

        # If a single sentence is huge, hard-split it.
        if len(s) > max_len:
            flush()
            start = 0
            while start < len(s) and len(chunks) < max_parts:
                chunks.append(s[start : start + max_len].strip())
                start += max_len
            continue

        candidate = " ".join(buf + [s]).strip()
        if len(candidate) <= max_len and (len(buf) + 1) <= max_sentences_per_chunk:
            buf.append(s)
        else:
            flush()
            buf.append(s)

        if len(chunks) >= max_parts:
            break

    if len(chunks) < max_parts:
        flush()

    # If we hit the cap, add a visible truncation marker.
    if chunks and len(chunks) == max_parts:
        chunks[-1] = chunks[-1].rstrip() + " …"

    return chunks[:max_parts]


async def send_split_message(
    channel: discord.abc.Messageable,
    text: str,
    *,
    max_len: int = 750,
    max_parts: int = 8,
    delay: float = 0.25,
) -> None:
    """Send `text` as multiple Discord messages using `split_for_discord`."""
    parts = split_for_discord(
        text,
        max_len=max_len,
        max_parts=max_parts,
        max_sentences_per_chunk=5,
    )

    for i, chunk in enumerate(parts):
        await channel.send(chunk)
        if delay and i != len(parts) - 1:
            await asyncio.sleep(delay)


# =========================
# Discord client setup
# =========================

intents = discord.Intents.default()
intents.message_content = True  # required to read message content

client = discord.Client(intents=intents)

# Persistent reminder store (survives restarts)
store = ReminderStore()


@client.event
async def on_ready() -> None:
    """
    Fired when the bot connects.

    We:
    - print an info line
    - start the reminder loop
    - announce readiness in allowed channels (once per process)
    """
    global READY_ANNOUNCED

    log(f"Logged in as {client.user} (ID: {client.user.id})")

    # Start background reminder scheduler
    asyncio.create_task(reminder_loop(client, store))

    # Announce readiness only once per process (avoid reconnect spam)
    if READY_ANNOUNCED:
        return
    READY_ANNOUNCED = True

    ready_msg = "✅ I’m online."
    for ch_id in ALLOWED_CHANNEL_IDS:
        try:
            channel = client.get_channel(ch_id) or await client.fetch_channel(ch_id)
            await channel.send(ready_msg)
        except Exception as e:
            log(f"Ready message failed for channel {ch_id}: {e}")


@client.event
async def on_message(message: discord.Message) -> None:
    """
    Main message handler.

    Order:
    1) Ignore self / empty messages
    2) Enforce allowed channels (return early)
    3) Route commands to commands.py (single source of truth)
    4) Otherwise: normal AI chat
    """
    # Ignore messages from the bot itself
    if message.author == client.user:
        return

    content = (message.content or "").strip()
    if not content:
        return

    # Channel restriction (hard gate)
    if message.channel.id not in ALLOWED_CHANNEL_IDS:
        return

    # -------------------------
    # 1) Commands
    # -------------------------
    # All command handling lives in commands.py.
    if content.startswith("!"):
        handled = await handle_commands(
            message,
            content,
            store=store,
            default_tz=DEFAULT_TZ,
            LongMemory=Long_Term_Memory,
        )
        if handled:
            return

    # -------------------------
    # 2) AI conversation
    # -------------------------
    # Remove bot mention (common when users ping the bot)
    user_text = content.replace(f"<@{client.user.id}>", "").strip()
    if not user_text:
        return

    username = message.author.display_name
    user_id = message.author.id

    log(f"{username}: {user_text}")

    try:
        # --- Per-user memory ---
        short_memory = get_short_memory(user_id)
        long_memory = Long_Term_Memory(user_id)  # keep long-term memory behavior unchanged

        # --- Emotion processing ---
        delta = analyze_input(user_text)
        emotion.apply(delta)

        # --- Update long-term memory ---
        long_memory.update_from_text(user_text)

        # --- Refresh system prompt (persona + emotion + time) ---
        short_memory.refresh_system()

        # --- Inject long-term facts as system info ---
        # Guard against empty/invalid short_memory.messages to avoid "list index out of range"
        if not hasattr(short_memory, "messages") or short_memory.messages is None:
            short_memory.messages = []

        if len(short_memory.messages) == 0:
            short_memory.messages.append({"role": "system", "content": ""})
        elif (
            not isinstance(short_memory.messages[0], dict)
            or short_memory.messages[0].get("role") != "system"
        ):
            short_memory.messages.insert(0, {"role": "system", "content": ""})

        facts = (long_memory.as_prompt() or "").rstrip(" |\n")
        if facts:
            short_memory.messages[0]["content"] += f"\n\nKnown facts:\n{facts}"

        # --- Add user message to short-term memory ---
        short_memory.add("user", user_text)

        # Build chat messages for the LLM
        messages = short_memory.get_messages()

        # ask_llama is synchronous; run it in a thread so Discord doesn't lag.
        async with message.channel.typing():
            reply = await asyncio.to_thread(ask_llama, messages)

        # Censor banned words in the AI reply before sending
        reply = filter_banned_words(reply)

        # Store assistant reply in short-term memory
        short_memory.add("assistant", reply)

        # Emotion decay over time (keeps mood from sticking forever)
        emotion.decay()

        log(f"AI > {username}: {reply}")
        await send_split_message(message.channel, reply)

        # Optional: auto-voice replies if the user enabled it via !voice on
        try:
            if get_voice_enabled(user_id, Long_Term_Memory):
                await maybe_speak_reply(message, reply)
        except Exception as e:
            # Voice is optional; don't let it break chat.
            log(f"[Voice] Failed: {e}")

        log(f"MOOD {emotion.value()} ({emotion.label()})")

    except Exception as e:
        log(f"Bot error: {e}")
        await message.channel.send("There is an issue with my AI.")


# =========================
# Start bot
# =========================

client.run(DISCORD_TOKEN)
