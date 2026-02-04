"""
Discord AI bot (chat + commands)

Key rules:
- The bot only responds in ALLOWED_CHANNEL_IDS.
- Reminders are created ONLY via the explicit `!reminder ...` command.
- Commands live in commands.py (single source of truth).
- Core conversation logic is in core/conversation.py.

Notes:
- ask_llama() is synchronous, so we run it in a thread to avoid blocking Discord's event loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import discord

# Local imports
from ai import ask_llama
from config import DISCORD_TOKEN, ALLOWED_CHANNEL_IDS

try:
    from config import RANDOM_ENGAGE_ENABLED, RANDOM_ENGAGE_MIN_MINUTES, RANDOM_ENGAGE_MAX_MINUTES
except ImportError:
    RANDOM_ENGAGE_ENABLED = False
    RANDOM_ENGAGE_MIN_MINUTES = 5
    RANDOM_ENGAGE_MAX_MINUTES = 10

from personality.memory_long import Long_Term_Memory
from reminders import ReminderStore, reminder_loop
from trust import trust
from commands import handle_commands
from utils.logging import log, DEFAULT_TZ
from utils.text import WordFilter, load_word_list
from utils.burst import enqueue_burst_message, set_burst_handler
from message_queue import message_queue
from core import handle_ai_conversation, set_word_filter, random_engage_loop
import uptime


# =========================
# Configuration
# =========================

# Resolve file paths relative to this script
BASE_DIR = Path(__file__).resolve().parent

# Path to the banned-words list (one lowercase word per line)
BANNED_WORDS_FILE = BASE_DIR / "banned_words.txt"

# Where we log censorship events
CENSOR_LOG_FILE = BASE_DIR / "logs" / "filtered_words.txt"

# Word filter instance
_word_filter = WordFilter(
    banned_words=load_word_list(BANNED_WORDS_FILE),
    log_file=CENSOR_LOG_FILE,
)

# Share word filter with core module
set_word_filter(_word_filter)

# Uptime tracker
uptime.TRACKER = uptime.UptimeTracker.start(DEFAULT_TZ)

# Persistent reminder store (survives restarts)
store = ReminderStore()

# State flags
_READY_ANNOUNCED = False
_TTS_READY = False


# =========================
# Discord client setup
# =========================

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)


async def _burst_to_queue_handler(
    message: discord.Message,
    user_text: str,
    raw_content: str = "",
) -> None:
    """
    Burst buffer calls this after combining rapid messages.
    We then queue the combined message for priority processing.
    """
    # Get trust for priority
    try:
        trust_score = trust.get(message.author.id)
    except Exception:
        trust_score = 0.5
    
    # Queue the message
    queued = await message_queue.enqueue_with_trust(
        message, user_text, trust_score, raw_content
    )
    
    if not queued:
        log(f"[Queue] Dropped message from {message.author.display_name} (queue full)")
        await message.channel.send("I'm a bit overwhelmed right now, try again in a moment!")


# Register burst handler (feeds into queue)
set_burst_handler(_burst_to_queue_handler)


# =========================
# Discord events
# =========================

@client.event
async def on_connect() -> None:
    if uptime.TRACKER:
        uptime.TRACKER.mark_connect()
        if uptime.TRACKER.reconnects > 0:
            log(f"[Uptime] Reconnected to Discord (#{uptime.TRACKER.reconnects}).")


@client.event
async def on_disconnect() -> None:
    if uptime.TRACKER:
        uptime.TRACKER.mark_disconnect()
    log("[Uptime] Disconnected from Discord.")


@client.event
async def on_resumed() -> None:
    if uptime.TRACKER:
        uptime.TRACKER.mark_resume()
    log("[Uptime] Session resumed.")


@client.event
async def on_ready() -> None:
    """Fired when the bot connects."""
    global _READY_ANNOUNCED, _TTS_READY
    
    log(f"Logged in as {client.user} (ID: {client.user.id})")
    
    # Start message queue worker
    await message_queue.start_worker(handle_ai_conversation)
    log("[Queue] Message queue worker started.")
    
    # Start background reminder scheduler
    asyncio.create_task(reminder_loop(client, store))
    
    # Start random engagement loop if enabled
    if RANDOM_ENGAGE_ENABLED:
        asyncio.create_task(
            random_engage_loop(
                client,
                ALLOWED_CHANNEL_IDS,
                ask_llama,
                _word_filter,
                min_minutes=RANDOM_ENGAGE_MIN_MINUTES,
                max_minutes=RANDOM_ENGAGE_MAX_MINUTES,
            )
        )
    
    # Optional: initialize edge-TTS
    if not _TTS_READY:
        _TTS_READY = True
        asyncio.create_task(_warmup_edge_tts())
    
    # Announce readiness only once per process
    if _READY_ANNOUNCED:
        return
    _READY_ANNOUNCED = True


async def _warmup_edge_tts() -> None:
    """Warm up Edge TTS (minimal - Edge TTS is cloud-based)."""
    log("[Voice] edge-TTS ready.")
    
    try:
        from tts_edge import warmup_tts
        await warmup_tts()
        log("I'm online and ready to chat!")
    except Exception as e:
        log(f"[Voice] edge-TTS import failed: {e}")


@client.event
async def on_message(message: discord.Message) -> None:
    """
    Main message handler.
    
    Order:
    1) Ignore self / empty messages
    2) Enforce allowed channels (return early)
    3) Route commands to commands.py
    4) Otherwise: queue for AI chat
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
    
    # Commands (HIGH priority - bypass queue)
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
    
    # AI conversation - queue with trust-based priority
    user_text = content.replace(f"<@{client.user.id}>", "").strip()
    if not user_text:
        return
    
    # Queue message (burst buffer feeds into queue)
    await enqueue_burst_message(message, user_text)


# =========================
# Start bot
# =========================

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
