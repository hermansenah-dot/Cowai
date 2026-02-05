"""
Context building utilities for Discord messages.
Provides:
- build_recent_context: Gathers recent channel history for context window.
- send_split_message: Sends long messages, split for Discord limits.
"""

from __future__ import annotations


import time
from typing import TYPE_CHECKING
from utils.errors import report_discord_error

import discord

if TYPE_CHECKING:
    pass

__all__ = ["build_recent_context", "send_split_message", "RECENT_CONTEXT_LIMIT"]

# Recent context limit for channel history
RECENT_CONTEXT_LIMIT = 5  # Changed from 4 - allow 5 messages of context

# Cache recent context per channel to avoid repeated API calls
# Format: {channel_id: (timestamp, context_list)}
_CONTEXT_CACHE: dict[int, tuple[float, list[dict]]] = {}
_CACHE_TTL = 2.0  # Cache valid for 2 seconds


async def build_recent_context(
    message: discord.Message,
    limit: int = RECENT_CONTEXT_LIMIT,
) -> list[dict]:
    """
    Pull recent channel history as additional context.
    
    - Uses short-lived cache to avoid repeated API calls
    - Skips bots and commands.
    - Skips messages by the SAME author to avoid duplicating burst parts.
    - Labels each line with speaker name (helps in busy channels).
    
    Returns: list of {role, content} (oldest -> newest).
    """
    channel_id = message.channel.id
    now = time.time()
    
    # Check cache
    if channel_id in _CONTEXT_CACHE:
        cached_time, cached_ctx = _CONTEXT_CACHE[channel_id]
        if now - cached_time < _CACHE_TTL:
            # Filter out messages from current author (may differ per call)
            return [m for m in cached_ctx if not m.get("_author_id") == message.author.id]
    
    ctx: list[dict] = []
    try:
        async for m in message.channel.history(limit=limit + 2, before=message):  # Fetch extra for filtering
            if m.author.bot:
                continue
            content = (m.content or "").strip()
            if not content or content.startswith("!"):
                continue
            ctx.append({
                "role": "user",
                "content": f"{m.author.display_name}: {content}",
                "_author_id": m.author.id,  # For filtering
            })
            if len(ctx) >= limit:
                break
        ctx.reverse()
        # Cache the full context
        _CONTEXT_CACHE[channel_id] = (now, ctx)
        # Evict old cache entries
        if len(_CONTEXT_CACHE) > 50:
            oldest_key = min(_CONTEXT_CACHE, key=lambda k: _CONTEXT_CACHE[k][0])
            del _CONTEXT_CACHE[oldest_key]
    except Exception as exc:
        await report_discord_error(message.channel, "Failed to build recent context.", exc)
        return []
    finally:
        pass
    
    # Return filtered (exclude current author)
    return [{"role": m["role"], "content": m["content"]} for m in ctx if m.get("_author_id") != message.author.id]


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
    
    try:
        await channel.send(text)
    except Exception as exc:
        await report_discord_error(channel, "Failed to send message to Discord.", exc)
