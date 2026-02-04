"""Context building utilities for Discord messages."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    pass

__all__ = ["build_recent_context", "send_split_message"]

# Recent context limit for channel history
RECENT_CONTEXT_LIMIT = 6


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
