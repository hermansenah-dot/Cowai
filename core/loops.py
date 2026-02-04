"""Background loops - random engagement and other periodic tasks."""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Callable, Awaitable

import discord

from utils.logging import log

if TYPE_CHECKING:
    from utils.text import WordFilter

__all__ = ["random_engage_loop"]

# Prompts for random engagement (the AI will riff on these)
_ENGAGE_PROMPTS = [
    "Start a casual conversation with chat. Maybe comment on something random, ask what everyone's up to, or share a quick thought.",
    "Say something playful to get chat's attention. Could be a random observation, a silly question, or just vibing.",
    "Engage the chat with a fun question or comment. Keep it light and conversational.",
    "Share a random thought or ask chat something interesting. Be yourself.",
    "Start some banter with chat. Maybe tease them gently or say something curious.",
]


async def random_engage_loop(
    client: discord.Client,
    allowed_channel_ids: set[int],
    ask_llama: Callable,
    word_filter: "WordFilter",
    *,
    min_minutes: float = 5.0,
    max_minutes: float = 10.0,
) -> None:
    """Background loop that sends random engagement messages at intervals."""
    log("[Engage] Random engagement loop started.")
    
    # Wait a bit after startup before first message
    await asyncio.sleep(60)
    
    while True:
        try:
            # Random wait between configured min and max minutes
            wait_minutes = random.uniform(min_minutes, max_minutes)
            wait_seconds = wait_minutes * 60
            log(f"[Engage] Next random message in {wait_minutes:.1f} minutes.")
            await asyncio.sleep(wait_seconds)
            
            # Pick a random allowed channel
            if not allowed_channel_ids:
                continue
            
            channel_id = random.choice(list(allowed_channel_ids))
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
                reply = word_filter.filter(reply)
                
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
