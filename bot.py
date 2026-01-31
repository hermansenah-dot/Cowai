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
from collections import defaultdict
from pathlib import Path
from datetime import datetime
from typing import Optional

import discord
import pytz
import uptime


# =========================
# Recent-context + burst buffering (human-like multi-message turns)
# =========================

RECENT_CONTEXT_LIMIT = 12

BURST_WINDOW_S = 2.5     # wait this long for more messages from same user in same channel
BURST_MAX_LINES = 6      # stop buffering after this many messages
BURST_MAX_CHARS = 900    # stop buffering after this many characters

# key = (channel_id, user_id)
_BURST: dict[tuple[int, int], dict] = {}
_BURST_LOCKS = defaultdict(asyncio.Lock)

async def build_recent_context(message: discord.Message, limit: int = RECENT_CONTEXT_LIMIT) -> list[dict]:
    """
    Pull recent channel history as additional context.
    - Skips bots and commands.
    - Skips messages by the SAME author as `message` to avoid duplicating burst parts.
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
        # If history fetching fails (permissions), just skip.
        return []
    return ctx

async def _finalize_burst(key: tuple[int, int]) -> tuple[discord.Message, str] | None:
    async with _BURST_LOCKS[key]:
        state = _BURST.pop(key, None)
        if not state:
            return None
        msg: discord.Message = state["last_message"]
        combined = "\n".join(state["lines"]).strip()
        if len(combined) > BURST_MAX_CHARS:
            combined = combined[:BURST_MAX_CHARS].strip()
        return msg, combined

async def _burst_worker(key: tuple[int, int]) -> None:
    """Debounce worker: waits for the user to stop sending messages, then replies once."""
    while True:
        async with _BURST_LOCKS[key]:
            state = _BURST.get(key)
            if not state:
                return
            ev: asyncio.Event = state["event"]
            ev.clear()

            # Safety caps: finalize early
            combined_now = "\n".join(state["lines"]).strip()
            if len(state["lines"]) >= BURST_MAX_LINES or len(combined_now) >= BURST_MAX_CHARS:
                break

        try:
            await asyncio.wait_for(ev.wait(), timeout=BURST_WINDOW_S)
            continue  # got another message; keep waiting
        except asyncio.TimeoutError:
            break  # burst ended

    finalized = await _finalize_burst(key)
    if not finalized:
        return
    msg, combined_text = finalized
    if not combined_text:
        return
    await handle_ai_conversation(msg, combined_text, raw_content=combined_text)

async def enqueue_burst_message(message: discord.Message, user_text: str) -> None:
    """
    Add a message to the user's burst buffer and ensure a worker is running.
    The worker will send ONE combined reply after the burst ends.
    """
    key = (message.channel.id, message.author.id)
    async with _BURST_LOCKS[key]:
        state = _BURST.get(key)
        if state is None:
            state = {
                "lines": [],
                "last_message": message,
                "event": asyncio.Event(),
                "task": None,
            }
            _BURST[key] = state

        # Add this line
        t = (user_text or "").strip()
        if t:
            state["lines"].append(t)
        state["last_message"] = message

        # Wake the worker
        state["event"].set()

        # Start worker if needed
        task = state.get("task")
        if task is None or task.done():
            state["task"] = asyncio.create_task(_burst_worker(key))


from ai import ask_llama
from config import DISCORD_TOKEN, ALLOWED_CHANNEL_IDS
from emotion import emotion
from personality.memory_long import Long_Term_Memory
from personality.memory_short import get_short_memory
from reminders import ReminderStore, reminder_loop
from triggers import analyze_input
from trust import trust
import humanize

# Centralized command router
from commands import handle_commands, maybe_auto_voice_reply
# =========================
# Configuration
# =========================

# Discord hard-limit is 2000 chars; we keep replies shorter for readability.
DISCORD_MAX = 2000

# Only respond in these channels (hard gate).
# Defined in config.py as ALLOWED_CHANNEL_IDS.

# Default timezone for reminders / timestamps.
DEFAULT_TZ = pytz.timezone("Europe/Copenhagen")

# Uptime tracker
uptime.TRACKER = uptime.UptimeTracker.start(DEFAULT_TZ)

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

# Prevent repeated coqui-TTS warmup on reconnects.
COQUI_WARMED_UP = False


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

# Reconnect tracker

@client.event
async def on_connect() -> None:
    if uptime.TRACKER:
        uptime.TRACKER.mark_connect()
        # Optional log for reconnects only:
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


# Persistent reminder store (survives restarts)
store = ReminderStore()


@client.event
async def on_ready() -> None:
    """
    Fired when the bot connects.

    We:
    - print an info line
    - start the reminder loop
    - announce readiness in the console (once per process)
    """
    global READY_ANNOUNCED
    global COQUI_WARMED_UP

    log(f"Logged in as {client.user} (ID: {client.user.id})")

    # Start background reminder scheduler
    asyncio.create_task(reminder_loop(client, store))

    # Optional: warm up coqui-TTS once so the first voice line is fast.
    if not COQUI_WARMED_UP:
        COQUI_WARMED_UP = True

        async def _coqui_warmup() -> None:
            log("[Voice] coqui-TTS warmup starting...")
            try:
                from tts_coqui import warmup_tts  # lazy / optional dependency
            except Exception as e:
                log(f"[Voice] coqui-TTS warmup skipped (import failed): {e}")
                return
            try:
                await warmup_tts()
                log("[Voice] coqui-TTS warmup complete.")
                log("✅ I’m online and ready to chat!")
            except Exception as e:
                log(f"[Voice] coqui-TTS warmup failed: {e}")

        asyncio.create_task(_coqui_warmup())

    # Announce readiness only once per process (avoid reconnect spam)
    if READY_ANNOUNCED:
        return
    READY_ANNOUNCED = True



async def handle_ai_conversation(message: discord.Message, user_text: str, raw_content: str = "") -> None:
    """Run the normal AI conversation logic for a (possibly combined) user_text."""
    username = message.author.display_name
    user_id = message.author.id

    log(f"{username}: {user_text}")

    try:
        # --- Per-user memory ---
        short_memory = get_short_memory(user_id)
        long_memory = Long_Term_Memory(user_id)  # keep long-term memory behavior unchanged

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

        # Higher trust => mood impacted more strongly by messages.
        # Lower trust => less reactive mood.
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

        mem_block = (long_memory.as_prompt(user_text) or "").strip()
        extras: list[str] = []

        # Trust (per-user)
        if trust_block:
            extras.append(trust_block)

        # Build a style hint from trust + emotion so the bot feels more human in conversation.
        try:
            m = emotion.metrics()
            relax = float(getattr(tstyle, "relax", 0.40)) if tstyle is not None else 0.40
            style = humanize.Style(
                relax=relax,
                mood_label=emotion.label(),
                valence=float(m.get("valence", 0.0)),
                arousal=float(m.get("arousal", 0.0)),
                dominance=float(m.get("dominance", 0.0)),
            )
        except Exception:
            style = humanize.Style(relax=0.40, mood_label=emotion.label())

        # Conversation rules (appended to system prompt via memory_short extras)
        extras.append(humanize.system_style_block(style))

        # Long-term memory block
        if mem_block:
            extras.append(mem_block)

        if extras and hasattr(short_memory, "set_system_extras"):
            try:
                short_memory.set_system_extras(extras)  # type: ignore[attr-defined]
            except Exception:
                pass

        # --- Record message to long-term store (for episodic extraction) ---
        try:
            long_memory.record_message("user", user_text)
        except Exception:
            pass

        # --- Add user message to short-term memory ---
        short_memory.add("user", user_text)

        # Build chat messages for the LLM
        base_messages = short_memory.get_messages()
        recent_ctx = await build_recent_context(message, limit=RECENT_CONTEXT_LIMIT)
        # Inject recent channel context after the system prompt
        messages = [base_messages[0], *recent_ctx, *base_messages[1:]]

        # ask_llama is synchronous; run it in a thread so Discord doesn't lag.
        async with message.channel.typing():
            reply = await asyncio.to_thread(ask_llama, messages)

        # Censor banned words in the AI reply before sending
        reply = filter_banned_words(reply)

        # Add a human-like conversational layer (listening line + occasional single follow-up)
        try:
            reply = humanize.apply_human_layer(reply, user_text, style)
        except Exception:
            pass

        # Store assistant reply in short-term memory
        short_memory.add("assistant", reply)

        # Record assistant reply to long-term store
        try:
            long_memory.record_message("assistant", reply)
        except Exception:
            pass

        # Periodically extract structured facts/episodes in the background
        try:
            asyncio.create_task(asyncio.to_thread(long_memory.maybe_extract, ask_llama))
        except Exception:
            pass

        # Emotion decay over time (keeps mood from sticking forever)
        emotion.decay()

        log(f"AI > {username}: {reply}")
        await send_split_message(message.channel, reply)

        # Optional: auto-voice replies if the user enabled it via !voice on
        try:
            await maybe_auto_voice_reply(message, reply, Long_Term_Memory)
        except Exception as e:
            # Voice is optional; don't let it break chat.
            log(f"[Voice] Failed: {e}")

        m = emotion.metrics()
        log(
            "MOOD "
            f"{emotion.label()} "
            f"(int={emotion.value():+d}, V={m['valence']:+.2f}, A={m['arousal']:+.2f}, D={m['dominance']:+.2f})"
        )

    except Exception as e:
        log(f"Bot error: {e}")
        await message.channel.send("There is an issue with my AI.")


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

    # Buffer rapid multi-message bursts into a single combined reply.
    # The worker will call handle_ai_conversation() once the user stops sending messages.
    await enqueue_burst_message(message, user_text)
    return


# =========================
# Start bot
# =========================

client.run(DISCORD_TOKEN)