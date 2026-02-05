"""
AI conversation handler - core message processing logic.
Handles:
- AI reply generation
- Persona and style integration
- Context and memory injection (calls core/context.py)
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import discord

from ai import ask_llama, analyze_nlp
from config.config import EMOTION_ENABLED, HUMANIZE_ENABLED
from core.mood import emotion
from personality.memory_long import Long_Term_Memory
from personality.memory_short import get_short_memory
from triggers import analyze_input
from core.mood import trust
from commands.voice import maybe_auto_voice_reply
from utils.logging import log, log_user, log_ai
from utils.errors import report_discord_error, wrap_discord_errors
from utils.text import WordFilter, load_word_list
from core.context import build_recent_context, send_split_message, RECENT_CONTEXT_LIMIT
import humanize

if TYPE_CHECKING:
    pass

__all__ = ["handle_ai_conversation", "set_word_filter"]

# Module-level word filter (set from bot.py)
_word_filter: WordFilter | None = None


def set_word_filter(word_filter: WordFilter) -> None:
    """Set the word filter instance (called from bot.py)."""
    global _word_filter
    _word_filter = word_filter


# =========================
# Response time tracking
# =========================

_response_times: list[float] = []
_RESPONSE_TIME_WINDOW = 5  # Log average every N messages


def _track_response_time(response_time: float) -> None:
    """Track response time and log average every 5 messages."""
    _response_times.append(response_time)
    
    if len(_response_times) >= _RESPONSE_TIME_WINDOW:
        avg = sum(_response_times) / len(_response_times)
        log(f"[Stats] Avg response time (last {_RESPONSE_TIME_WINDOW}): {avg:.2f}s")
        _response_times.clear()


# =========================
# Async NLP cache
# =========================

# We compute NLP in the background and apply it on the *next* turn.
_NLP_HINT_CACHE: dict[int, str] = {}
_NLP_INFLIGHT: set[int] = set()


@wrap_discord_errors
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
        # --- Per-user memory ---
        short_memory = get_short_memory(user_id)
        long_memory = Long_Term_Memory(user_id)

        # --- Trust context (per-user) ---
        tstyle = None
        trust_block = None
        try:
            tstyle = trust.style(user_id)
            trust_block = trust.prompt_block(user_id)
        except Exception as exc:
            await report_discord_error(message.channel, "Failed to load trust context.", exc)

        # --- Emotion processing ---
        if EMOTION_ENABLED:
            delta = analyze_input(user_text)
            # Higher trust => mood impacted more strongly by messages
            if tstyle is not None:
                try:
                    delta = int(round(float(delta) * float(tstyle.mood_multiplier)))
                except Exception as exc:
                    await report_discord_error(message.channel, "Failed to apply mood multiplier.", exc)
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
        if analyze_nlp is not None:
            try:
                cached = _NLP_HINT_CACHE.get(user_id, "")
                if cached:
                    extras.append(cached)
            except Exception as exc:
                await report_discord_error(message.channel, "Failed to cache NLP hint.", exc)

        if extras and hasattr(short_memory, "set_system_extras"):
            try:
                short_memory.set_system_extras(extras)
            except Exception as exc:
                await report_discord_error(message.channel, "Failed to set system extras.", exc)

        # --- Record message to long-term store ---
        try:
            long_memory.record_message("user", user_text)
        except Exception as exc:
            await report_discord_error(message.channel, "Failed to record user message.", exc)

        # --- Add user message to short-term memory ---
        short_memory.add("user", user_text)

        # Build chat messages for the LLM
        base_messages = short_memory.get_messages()
        recent_ctx = await build_recent_context(message, limit=RECENT_CONTEXT_LIMIT)
        messages = [base_messages[0]] + recent_ctx + base_messages[1:]

        # ask_llama is synchronous; run it in a thread
        async with message.channel.typing():
            start_time = time.perf_counter()
            reply = await asyncio.to_thread(ask_llama, messages)
            response_time = time.perf_counter() - start_time

        # Track response time stats
        _track_response_time(response_time)

        # Filter banned words
        if _word_filter:
            reply = _word_filter.filter(reply)

        # Add human-like conversational layer
        if HUMANIZE_ENABLED:
            try:
                reply = humanize.apply_human_layer(reply, user_text, style)
            except Exception as exc:
                await report_discord_error(message.channel, "Failed to apply human layer.", exc)

        # Store assistant reply in memory
        short_memory.add("assistant", reply)
        try:
            long_memory.record_message("assistant", reply)
        except Exception as exc:
            await report_discord_error(message.channel, "Failed to record assistant message.", exc)

        # Periodically extract structured facts/episodes in the background
        try:
            asyncio.create_task(asyncio.to_thread(long_memory.maybe_extract, ask_llama))
        except Exception as exc:
            await report_discord_error(message.channel, "Failed to extract memory.", exc)

        # --- NLP analysis (async, for next turn) ---
        if analyze_nlp is not None:
            try:
                ctx_for_nlp = []
                try:
                    ctx_for_nlp = [m for m in getattr(short_memory, "messages", [])[1:] if isinstance(m, dict)][-6:]
                except Exception:
                    ctx_for_nlp = []
                asyncio.create_task(_update_nlp_hint(user_id, user_text, ctx_for_nlp))
            except Exception as exc:
                await report_discord_error(message.channel, "Failed to update NLP hint.", exc)

        # Emotion decay over time
        if EMOTION_ENABLED:
            emotion.decay()

        log_ai(f"({response_time:.2f}s) AI > {username}: {reply}")
        await send_split_message(message.channel, reply)
        # Optional: auto-voice replies
        try:
            await maybe_auto_voice_reply(message, reply)
        except Exception as exc:
            await report_discord_error(message.channel, "Voice reply failed.", exc)
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
        pass
    finally:
        _NLP_INFLIGHT.discard(user_id)


def _nlp_system_hint(nlp: dict) -> str:
    """Convert NLP analysis into a short INTERNAL hint for the system prompt."""
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

    return "INTERNAL NLP HINT (do not quote): " + "; ".join(bits)


def _build_conversation_style(tstyle) -> humanize.Style:
    """Build a Style object from trust and emotion state."""
    relax = float(getattr(tstyle, "relax", 0.40)) if tstyle is not None else 0.40
    
    if not EMOTION_ENABLED:
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
        return
    m = emotion.metrics()
    log(
        f"MOOD {emotion.label()} "
        f"(int={emotion.value():+d}, V={m['valence']:+.2f}, "
        f"A={m['arousal']:+.2f}, D={m['dominance']:+.2f})"
    )
