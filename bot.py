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
try:
    from ai import ask_llama, analyze_nlp
except Exception:
    from ai import ask_llama
    analyze_nlp = None  # type: ignore

from config import DISCORD_TOKEN, ALLOWED_CHANNEL_IDS, EMOTION_ENABLED, HUMANIZE_ENABLED
try:
    from config import RANDOM_ENGAGE_ENABLED, RANDOM_ENGAGE_MIN_MINUTES, RANDOM_ENGAGE_MAX_MINUTES
except ImportError:
    RANDOM_ENGAGE_ENABLED = False
    RANDOM_ENGAGE_MIN_MINUTES = 5
    RANDOM_ENGAGE_MAX_MINUTES = 10
import random
from emotion import emotion
from personality.memory_long import Long_Term_Memory
from personality.memory_short import get_short_memory
from reminders import ReminderStore, reminder_loop
from triggers import analyze_input
from trust import trust
from commands import handle_commands, maybe_auto_voice_reply
from utils.logging import log, log_user, log_ai, DEFAULT_TZ
from utils.text import WordFilter, load_word_list, split_for_discord
from utils.burst import enqueue_burst_message, set_burst_handler
from message_queue import message_queue, Priority
import humanize
import uptime

if TYPE_CHECKING:
    pass


# =========================
# Configuration
# =========================

# Recent context limit for channel history
RECENT_CONTEXT_LIMIT = 6

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
_TTS_READY = False

# =========================
# Async NLP cache
# =========================

# We compute NLP in the background and apply it on the *next* turn.
_NLP_HINT_CACHE: dict[int, str] = {}
_NLP_INFLIGHT: set[int] = set()


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
    max_len: int = 2000,
    max_parts: int = 1,
    delay: float = 0.0,
) -> None:
    """Send `text` as a single Discord message (truncates if over 2000 chars)."""
    text = text.strip()
    if not text:
        return
    
    # Truncate to Discord's limit if needed
    if len(text) > max_len:
        text = text[:max_len - 3] + "..."
    
    await channel.send(text)


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
    
    log_user(f"{username}: {user_text}")
    
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
        if EMOTION_ENABLED:
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
        if HUMANIZE_ENABLED:
            extras.append(humanize.system_style_block(style))
        
        # Long-term memory block
        if mem_block:
            extras.append(mem_block)
        
        
        # --- NLP hint (async) ---
        # NLP is computed in the background to keep replies fast.
        # We apply the last cached hint on the next turn.
        if analyze_nlp is not None:
            try:
                cached = _NLP_HINT_CACHE.get(user_id, "")
                if cached:
                    extras.append(cached)
            except Exception:
                pass

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
        # Put recent context BEFORE user's conversation history, but after system prompt
        # This keeps the user's direct conversation coherent at the end
        messages = [base_messages[0]] + recent_ctx + base_messages[1:]
        
        # ask_llama is synchronous; run it in a thread
        async with message.channel.typing():
            reply = await asyncio.to_thread(ask_llama, messages)
        
        # Filter banned words
        reply = _word_filter.filter(reply)
        
        # Add human-like conversational layer
        if HUMANIZE_ENABLED:
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

        # --- NLP analysis (async, for next turn) ---
        if analyze_nlp is not None:
            try:
                ctx_for_nlp = []
                try:
                    ctx_for_nlp = [m for m in getattr(short_memory, "messages", [])[1:] if isinstance(m, dict)][-6:]
                except Exception:
                    ctx_for_nlp = []
                asyncio.create_task(_update_nlp_hint(user_id, user_text, ctx_for_nlp))
            except Exception:
                pass
        
        # Emotion decay over time
        if EMOTION_ENABLED:
            emotion.decay()
        
        log_ai(f"AI > {username}: {reply}")
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


async def _update_nlp_hint(user_id: int, user_text: str, ctx_for_nlp: list[dict]) -> None:
    """Compute NLP in the background and cache a short system hint for next turn."""
    if analyze_nlp is None:
        return
    if user_id in _NLP_INFLIGHT:
        return

    _NLP_INFLIGHT.add(user_id)
    try:
        # Keep it snappy; if the classifier is slow, we just skip this turn.
        nlp = await asyncio.wait_for(
            asyncio.to_thread(analyze_nlp, user_text, ctx_for_nlp),
            timeout=8,
        )
        hint = _nlp_system_hint(nlp)
        if hint:
            _NLP_HINT_CACHE[user_id] = hint
        else:
            _NLP_HINT_CACHE.pop(user_id, None)
    except Exception:
        # Never break chat because NLP failed.
        pass
    finally:
        _NLP_INFLIGHT.discard(user_id)



def _nlp_system_hint(nlp: dict) -> str:
    """
    Convert NLP analysis into a short INTERNAL hint for the system prompt.
    This should be compact and should NOT look like a transcript/telemetry header.
    """
    if not isinstance(nlp, dict):
        return ""
    intent = str(nlp.get("intent", "")).strip().lower()
    topic = str(nlp.get("topic", "")).strip()
    emo = nlp.get("emotion", {}) if isinstance(nlp.get("emotion", {}), dict) else {}
    label = str(emo.get("label", "")).strip().lower()
    needs = nlp.get("needs", [])
    if isinstance(needs, list):
        needs_s = ", ".join([str(x) for x in needs if str(x).strip()])
    else:
        needs_s = ""

    bits = []
    if intent:
        bits.append(f"intent={intent}")
    if topic:
        bits.append(f"topic={topic}")
    if label:
        bits.append(f"emotion={label}")
    if needs_s:
        bits.append(f"needs={needs_s}")

    if not bits:
        return ""

    # Avoid keywords that trigger ai.py telemetry sanitization.
    # Keep it as a single short block.
    return "INTERNAL NLP HINT (do not quote): " + "; ".join(bits)


def _build_conversation_style(tstyle) -> humanize.Style:
    """Build a Style object from trust and emotion state."""
    relax = float(getattr(tstyle, "relax", 0.40)) if tstyle is not None else 0.40
    
    if not EMOTION_ENABLED:
        # Return neutral style when emotion is disabled
        return humanize.Style(relax=relax, mood_label="neutral")
    
    try:
        m = emotion.metrics()
        return humanize.Style(
            relax=relax,
            mood_label=emotion.label(),
            valence=float(m.get("valence", 0.0)),
            arousal=float(m.get("arousal", 0.0)),
            dominance=float(m.get("dominance", 0.0)),
        )
    except Exception:
        return humanize.Style(relax=relax, mood_label="neutral")


def _log_mood_state() -> None:
    """Log current mood state to console."""
    if not EMOTION_ENABLED:
        return  # Skip logging when emotion is disabled
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


async def _burst_to_queue_handler(
    message: "discord.Message",
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
        asyncio.create_task(_random_engage_loop())
    
    # Optional: initialize edge-TTS
    if not _TTS_READY:
        _TTS_READY = True
        asyncio.create_task(_warmup_edge_tts())
    
    # Announce readiness only once per process
    if _READY_ANNOUNCED:
        return
    _READY_ANNOUNCED = True


# =========================
# Random Engagement Loop
# =========================

# Prompts for random engagement (the AI will riff on these)
_ENGAGE_PROMPTS = [
    "Start a casual conversation with chat. Maybe comment on something random, ask what everyone's up to, or share a quick thought.",
    "Say something playful to get chat's attention. Could be a random observation, a silly question, or just vibing.",
    "Engage the chat with a fun question or comment. Keep it light and conversational.",
    "Share a random thought or ask chat something interesting. Be yourself.",
    "Start some banter with chat. Maybe tease them gently or say something curious.",
]


async def _random_engage_loop() -> None:
    """Background loop that sends random engagement messages at intervals."""
    log("[Engage] Random engagement loop started.")
    
    # Wait a bit after startup before first message
    await asyncio.sleep(60)
    
    while True:
        try:
            # Random wait between configured min and max minutes
            wait_minutes = random.uniform(RANDOM_ENGAGE_MIN_MINUTES, RANDOM_ENGAGE_MAX_MINUTES)
            wait_seconds = wait_minutes * 60
            log(f"[Engage] Next random message in {wait_minutes:.1f} minutes.")
            await asyncio.sleep(wait_seconds)
            
            # Pick a random allowed channel
            if not ALLOWED_CHANNEL_IDS:
                continue
            
            channel_id = random.choice(list(ALLOWED_CHANNEL_IDS))
            channel = client.get_channel(channel_id)
            
            if channel is None:
                log(f"[Engage] Could not find channel {channel_id}")
                continue
            
            # Generate an engaging message using the AI
            prompt = random.choice(_ENGAGE_PROMPTS)
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": "(System: generate a single casual message for chat)"},
            ]
            
            try:
                reply = await asyncio.to_thread(ask_llama, messages)
                reply = _word_filter.filter(reply)
                
                if reply and len(reply.strip()) > 0:
                    await channel.send(reply)
                    log(f"[Engage] Sent random message to #{channel.name}: {reply[:50]}...")
            except Exception as e:
                log(f"[Engage] Failed to generate/send message: {e}")
                
        except asyncio.CancelledError:
            log("[Engage] Random engagement loop cancelled.")
            break
        except Exception as e:
            log(f"[Engage] Loop error: {e}")
            await asyncio.sleep(60)  # Wait a bit before retrying


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
    
    # Get trust score for priority
    try:
        trust_score = trust.get(message.author.id)
    except Exception:
        trust_score = 0.5
    
    # Queue message (burst buffer feeds into queue)
    await enqueue_burst_message(message, user_text)


# =========================
# Start bot
# =========================

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
