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
from pathlib import Path
from typing import TYPE_CHECKING

import discord
import pytz

# Local imports
from ai import ask_llama
from config import DISCORD_TOKEN, ALLOWED_CHANNEL_IDS
from emotion import emotion
from personality.memory_long import Long_Term_Memory
from personality.memory_short import get_short_memory
from reminders import ReminderStore, reminder_loop
from triggers import analyze_input
from trust import trust
from commands import handle_commands, maybe_auto_voice_reply
from utils.logging import log, DEFAULT_TZ
from utils.text import WordFilter, load_word_list, split_for_discord
from utils.burst import enqueue_burst_message, set_burst_handler
import humanize
import uptime

if TYPE_CHECKING:
    pass


# =========================
# Configuration
# =========================

# Recent context limit for channel history
RECENT_CONTEXT_LIMIT = 12

# Discord hard-limit is 2000 chars; we keep replies shorter for readability
DISCORD_MAX = 2000

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

# Uptime tracker
uptime.TRACKER = uptime.UptimeTracker.start(DEFAULT_TZ)

# Persistent reminder store (survives restarts)
store = ReminderStore()

# State flags
_READY_ANNOUNCED = False
_COQUI_WARMED_UP = False


# =========================
# Discord channel context
# =========================

async def build_recent_context(
    message: discord.Message,
    limit: int = RECENT_CONTEXT_LIMIT,
) -> list[dict]:
    """
    Pull recent channel history as additional context.
    
    - Skips bots and commands.
    - Skips messages by the SAME author to avoid duplicating burst parts.
    - Labels each line with speaker name (helps in busy channels).
    
    Returns: list of {role, content} (oldest -> newest).
    """
    ctx: list[dict] = []
    try:
        async for m in message.channel.history(limit=limit, before=message):
            if m.author.bot:
                continue
            if m.author.id == message.author.id:
                continue
            content = (m.content or "").strip()
            if not content:
                continue
            if content.startswith("!"):
                continue
            ctx.append({"role": "user", "content": f"{m.author.display_name}: {content}"})
        ctx.reverse()
    except Exception:
        # If history fetching fails (permissions), just skip
        return []
    return ctx


# =========================
# Message sending utilities
# =========================

async def send_split_message(
    channel: discord.abc.Messageable,
    text: str,
    *,
    max_len: int = 750,
    max_parts: int = 8,
    delay: float = 0.25,
) -> None:
    """Send `text` as multiple Discord messages using split_for_discord."""
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
# AI conversation handler
# =========================

async def handle_ai_conversation(
    message: discord.Message,
    user_text: str,
    raw_content: str = "",
) -> None:
    """Run the normal AI conversation logic for a (possibly combined) user_text."""
    username = message.author.display_name
    user_id = message.author.id
    
    log(f"{username}: {user_text}")
    
    try:
        # --- Per-user memory ---
        short_memory = get_short_memory(user_id)
        long_memory = Long_Term_Memory(user_id)
        
        # --- Trust context (per-user) ---
        tstyle = None
        trust_block = None
        try:
            tstyle = trust.style(user_id)
            trust_block = trust.prompt_block(user_id)
        except Exception:
            pass
        
        # --- Emotion processing ---
        delta = analyze_input(user_text)
        
        # Higher trust => mood impacted more strongly by messages
        if tstyle is not None:
            try:
                delta = int(round(float(delta) * float(tstyle.mood_multiplier)))
            except Exception:
                pass
        emotion.apply(delta)
        
        # --- Update long-term memory (fast rules) ---
        long_memory.update_from_text(user_text)
        
        # --- Refresh system prompt (persona + emotion + time) ---
        short_memory.refresh_system()
        
        # --- Inject long-term facts as system info ---
        _ensure_system_message(short_memory)
        
        mem_block = (long_memory.as_prompt(user_text) or "").strip()
        extras: list[str] = []
        
        # Trust (per-user)
        if trust_block:
            extras.append(trust_block)
        
        # Build a style hint from trust + emotion
        style = _build_conversation_style(tstyle)
        
        # Conversation rules (appended to system prompt via memory_short extras)
        extras.append(humanize.system_style_block(style))
        
        # Long-term memory block
        if mem_block:
            extras.append(mem_block)
        
        if extras and hasattr(short_memory, "set_system_extras"):
            try:
                short_memory.set_system_extras(extras)
            except Exception:
                pass
        
        # --- Record message to long-term store ---
        try:
            long_memory.record_message("user", user_text)
        except Exception:
            pass
        
        # --- Add user message to short-term memory ---
        short_memory.add("user", user_text)
        
        # Build chat messages for the LLM
        base_messages = short_memory.get_messages()
        recent_ctx = await build_recent_context(message, limit=RECENT_CONTEXT_LIMIT)
        messages = [base_messages[0], *recent_ctx, *base_messages[1:]]
        
        # ask_llama is synchronous; run it in a thread
        async with message.channel.typing():
            reply = await asyncio.to_thread(ask_llama, messages)
        
        # Filter banned words
        reply = _word_filter.filter(reply)
        
        # Add human-like conversational layer
        try:
            reply = humanize.apply_human_layer(reply, user_text, style)
        except Exception:
            pass
        
        # Store assistant reply in memory
        short_memory.add("assistant", reply)
        
        try:
            long_memory.record_message("assistant", reply)
        except Exception:
            pass
        
        # Periodically extract structured facts/episodes in the background
        try:
            asyncio.create_task(asyncio.to_thread(long_memory.maybe_extract, ask_llama))
        except Exception:
            pass
        
        # Emotion decay over time
        emotion.decay()
        
        log(f"AI > {username}: {reply}")
        await send_split_message(message.channel, reply)
        
        # Optional: auto-voice replies
        try:
            await maybe_auto_voice_reply(message, reply, Long_Term_Memory)
        except Exception as e:
            log(f"[Voice] Failed: {e}")
        
        _log_mood_state()
        
    except Exception as e:
        log(f"Bot error: {e}")
        await message.channel.send("There is an issue with my AI.")


def _ensure_system_message(short_memory) -> None:
    """Ensure short_memory has a valid system message at index 0."""
    if not hasattr(short_memory, "messages") or short_memory.messages is None:
        short_memory.messages = []
    
    if len(short_memory.messages) == 0:
        short_memory.messages.append({"role": "system", "content": ""})
    elif (
        not isinstance(short_memory.messages[0], dict)
        or short_memory.messages[0].get("role") != "system"
    ):
        short_memory.messages.insert(0, {"role": "system", "content": ""})


def _build_conversation_style(tstyle) -> humanize.Style:
    """Build a Style object from trust and emotion state."""
    try:
        m = emotion.metrics()
        relax = float(getattr(tstyle, "relax", 0.40)) if tstyle is not None else 0.40
        return humanize.Style(
            relax=relax,
            mood_label=emotion.label(),
            valence=float(m.get("valence", 0.0)),
            arousal=float(m.get("arousal", 0.0)),
            dominance=float(m.get("dominance", 0.0)),
        )
    except Exception:
        return humanize.Style(relax=0.40, mood_label=emotion.label())


def _log_mood_state() -> None:
    """Log current mood state to console."""
    m = emotion.metrics()
    log(
        f"MOOD {emotion.label()} "
        f"(int={emotion.value():+d}, V={m['valence']:+.2f}, "
        f"A={m['arousal']:+.2f}, D={m['dominance']:+.2f})"
    )


# =========================
# Discord client setup
# =========================

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

# Register burst handler
set_burst_handler(handle_ai_conversation)


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
    global _READY_ANNOUNCED, _COQUI_WARMED_UP
    
    log(f"Logged in as {client.user} (ID: {client.user.id})")
    
    # Start background reminder scheduler
    asyncio.create_task(reminder_loop(client, store))
    
    # Optional: warm up coqui-TTS once
    if not _COQUI_WARMED_UP:
        _COQUI_WARMED_UP = True
        asyncio.create_task(_warmup_coqui_tts())
    
    # Announce readiness only once per process
    if _READY_ANNOUNCED:
        return
    _READY_ANNOUNCED = True


async def _warmup_coqui_tts() -> None:
    """Warm up Coqui TTS for faster first voice line."""
    log("[Voice] coqui-TTS warmup starting...")
    
    def _do_warmup():
        """Synchronous warmup in thread to avoid blocking event loop."""
        try:
            from tts_coqui import warmup_tts
            return warmup_tts
        except Exception as e:
            raise RuntimeError(f"import failed: {e}") from e
    
    try:
        # Import in a thread to avoid blocking the event loop
        warmup_tts = await asyncio.to_thread(_do_warmup)
    except Exception as e:
        log(f"[Voice] coqui-TTS warmup skipped ({e})")
        return
    
    try:
        await warmup_tts()
        log("[Voice] coqui-TTS warmup complete.")
        log("I'm online and ready to chat!")
    except Exception as e:
        log(f"[Voice] coqui-TTS warmup failed: {e}")


@client.event
async def on_message(message: discord.Message) -> None:
    """
    Main message handler.
    
    Order:
    1) Ignore self / empty messages
    2) Enforce allowed channels (return early)
    3) Route commands to commands.py
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
    
    # Commands
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
    
    # AI conversation
    user_text = content.replace(f"<@{client.user.id}>", "").strip()
    if not user_text:
        return
    
    # Buffer rapid multi-message bursts
    await enqueue_burst_message(message, user_text)


# =========================
# Start bot
# =========================

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
