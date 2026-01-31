"""Burst buffering for multi-message user input.

This module handles the "human-like" behavior where rapid consecutive messages
from the same user are combined into a single AI request.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    import discord

# Configuration
BURST_WINDOW_S: float = 2.5   # Wait this long for more messages
BURST_MAX_LINES: int = 6      # Stop buffering after this many messages
BURST_MAX_CHARS: int = 900    # Stop buffering after this many characters


class BurstBuffer:
    """Manages burst buffering for multi-message user input.
    
    When users send rapid consecutive messages, this buffers them into
    a single combined message before processing.
    """
    
    def __init__(
        self,
        window_s: float = BURST_WINDOW_S,
        max_lines: int = BURST_MAX_LINES,
        max_chars: int = BURST_MAX_CHARS,
    ):
        self.window_s = window_s
        self.max_lines = max_lines
        self.max_chars = max_chars
        
        # State: key = (channel_id, user_id)
        self._bursts: dict[tuple[int, int], dict] = {}
        self._locks: dict[tuple[int, int], asyncio.Lock] = defaultdict(asyncio.Lock)
        
        # Callback for when burst is complete
        self._on_complete: Callable[..., Awaitable] | None = None
    
    def set_handler(self, handler: Callable[..., Awaitable]) -> None:
        """Set the callback to invoke when a burst is complete.
        
        Handler signature: async def handler(message, combined_text, raw_content)
        """
        self._on_complete = handler
    
    async def enqueue(self, message: "discord.Message", user_text: str) -> None:
        """Add a message to the user's burst buffer.
        
        Ensures a worker is running that will send ONE combined reply
        after the burst ends.
        """
        key = (message.channel.id, message.author.id)
        
        async with self._locks[key]:
            state = self._bursts.get(key)
            if state is None:
                state = {
                    "lines": [],
                    "last_message": message,
                    "event": asyncio.Event(),
                    "task": None,
                }
                self._bursts[key] = state
            
            # Add this line
            text = (user_text or "").strip()
            if text:
                state["lines"].append(text)
            state["last_message"] = message
            
            # Wake the worker
            state["event"].set()
            
            # Start worker if needed
            task = state.get("task")
            if task is None or task.done():
                state["task"] = asyncio.create_task(self._worker(key))
    
    async def _finalize(self, key: tuple[int, int]) -> tuple | None:
        """Finalize and return the burst data."""
        async with self._locks[key]:
            state = self._bursts.pop(key, None)
            if not state:
                return None
            
            msg = state["last_message"]
            combined = "\n".join(state["lines"]).strip()
            if len(combined) > self.max_chars:
                combined = combined[:self.max_chars].strip()
            
            return msg, combined
    
    async def _worker(self, key: tuple[int, int]) -> None:
        """Debounce worker: waits for the user to stop sending messages."""
        while True:
            async with self._locks[key]:
                state = self._bursts.get(key)
                if not state:
                    return
                
                event: asyncio.Event = state["event"]
                event.clear()
                
                # Safety caps: finalize early if limits reached
                combined_now = "\n".join(state["lines"]).strip()
                if len(state["lines"]) >= self.max_lines or len(combined_now) >= self.max_chars:
                    break
            
            try:
                await asyncio.wait_for(event.wait(), timeout=self.window_s)
                continue  # Got another message; keep waiting
            except asyncio.TimeoutError:
                break  # Burst ended
        
        finalized = await self._finalize(key)
        if not finalized:
            return
        
        msg, combined_text = finalized
        if not combined_text:
            return
        
        if self._on_complete:
            await self._on_complete(msg, combined_text, raw_content=combined_text)


# Global instance for backwards compatibility
_burst_buffer = BurstBuffer()


async def enqueue_burst_message(message: "discord.Message", user_text: str) -> None:
    """Add a message to the burst buffer (backwards-compatible API)."""
    await _burst_buffer.enqueue(message, user_text)


def set_burst_handler(handler: Callable[..., Awaitable]) -> None:
    """Set the burst completion handler (backwards-compatible API)."""
    _burst_buffer.set_handler(handler)
