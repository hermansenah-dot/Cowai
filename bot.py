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
import logging
from pathlib import Path

import discord
import time

# Suppress noisy voice_recv logs
logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.WARNING)

# Local imports
from ai import ask_llama
from config.config import DISCORD_TOKEN, ALLOWED_CHANNEL_IDS

try:
    from config.config import RANDOM_ENGAGE_ENABLED, RANDOM_ENGAGE_MIN_MINUTES, RANDOM_ENGAGE_MAX_MINUTES
except ImportError:
    RANDOM_ENGAGE_ENABLED = False
    RANDOM_ENGAGE_MIN_MINUTES = 5
    RANDOM_ENGAGE_MAX_MINUTES = 10

from personality.memory_long import Long_Term_Memory
from reminders import ReminderStore, reminder_loop
from core.mood import trust
from commands_main import handle_commands
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

# Use VoiceRecvClient for voice receive support
try:
    from discord.ext.voice_recv import VoiceRecvClient
    client = discord.Client(intents=intents, voice_client_class=VoiceRecvClient)
    log("[Bot] VoiceRecvClient enabled for STT support")
except ImportError:
    client = discord.Client(intents=intents)
    log("[Bot] VoiceRecvClient not available, STT disabled")


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
        from voice.tts import warmup_tts
        await warmup_tts()
        log("I'm online and ready to chat!")
    except Exception as e:
        log(f"[Voice] edge-TTS import failed: {e}")


from core.handlers import on_message as core_on_message

@client.event
async def on_message(message: discord.Message) -> None:
    await core_on_message(
        message,
        client,
        store,
        DEFAULT_TZ,
        Long_Term_Memory,
        enqueue_burst_message,
        ALLOWED_CHANNEL_IDS
    )


# =========================
# Start bot
# =========================

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
