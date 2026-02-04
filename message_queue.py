"""message_queue.py

Priority queue system for Discord messages.

Features:
- Priority levels: CRITICAL > HIGH > NORMAL > LOW
- Single worker prevents Ollama overload
- Integrates with burst buffer (sits after it)
- Trust-based priority boost for trusted users

Usage:
    from message_queue import message_queue, Priority
    
    # Queue a message for processing
    await message_queue.enqueue(message, user_text, priority=Priority.NORMAL)
    
    # Start the worker (call once in on_ready)
    asyncio.create_task(message_queue.start_worker(handler_func))
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Awaitable, Optional, TYPE_CHECKING
from time import time

if TYPE_CHECKING:
    import discord


class Priority(IntEnum):
    """Message priority levels (lower number = higher priority)."""
    CRITICAL = 0  # System messages, errors
    HIGH = 1      # Commands, trusted users
    NORMAL = 2    # Regular chat
    LOW = 3       # Background tasks, random engagement


@dataclass(order=True)
class QueueItem:
    """A queued message with priority ordering."""
    priority: int
    timestamp: float = field(compare=True)
    message: "discord.Message" = field(compare=False)
    user_text: str = field(compare=False)
    raw_content: str = field(compare=False, default="")


class MessageQueue:
    """
    Priority queue for Discord messages with a single worker.
    
    Messages are processed one at a time in priority order.
    This prevents Ollama from being overwhelmed by concurrent requests.
    """
    
    def __init__(self, max_size: int = 100):
        self._queue: asyncio.PriorityQueue[QueueItem] = asyncio.PriorityQueue(maxsize=max_size)
        self._handler: Optional[Callable[["discord.Message", str, str], Awaitable[None]]] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._running = False
        self._processing = False
        
        # Stats
        self.processed_count = 0
        self.dropped_count = 0
    
    async def enqueue(
        self,
        message: "discord.Message",
        user_text: str,
        priority: Priority = Priority.NORMAL,
        raw_content: str = "",
    ) -> bool:
        """
        Add a message to the queue.
        
        Returns True if queued, False if queue is full.
        """
        item = QueueItem(
            priority=priority.value,
            timestamp=time(),
            message=message,
            user_text=user_text,
            raw_content=raw_content,
        )
        
        try:
            self._queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            self.dropped_count += 1
            return False
    
    async def enqueue_with_trust(
        self,
        message: "discord.Message",
        user_text: str,
        trust_score: float = 0.0,
        raw_content: str = "",
    ) -> bool:
        """
        Queue with automatic priority based on trust score.
        
        Trust >= 0.7 -> HIGH priority
        Trust >= 0.4 -> NORMAL priority
        Trust < 0.4  -> LOW priority
        """
        if trust_score >= 0.7:
            priority = Priority.HIGH
        elif trust_score >= 0.4:
            priority = Priority.NORMAL
        else:
            priority = Priority.LOW
        
        return await self.enqueue(message, user_text, priority, raw_content)
    
    def set_handler(
        self,
        handler: Callable[["discord.Message", str, str], Awaitable[None]],
    ) -> None:
        """Set the message handler function."""
        self._handler = handler
    
    async def start_worker(
        self,
        handler: Optional[Callable[["discord.Message", str, str], Awaitable[None]]] = None,
    ) -> None:
        """Start the queue worker. Call this once in on_ready."""
        if handler:
            self._handler = handler
        
        if self._handler is None:
            raise ValueError("No handler set for message queue")
        
        if self._running:
            return
        
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
    
    async def stop_worker(self) -> None:
        """Stop the queue worker gracefully."""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
    
    async def _worker_loop(self) -> None:
        """Main worker loop - processes messages one at a time."""
        while self._running:
            try:
                # Wait for next item (blocks until available)
                item = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=1.0,  # Check running flag periodically
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            
            try:
                self._processing = True
                await self._handler(item.message, item.user_text, item.raw_content)
                self.processed_count += 1
            except Exception as e:
                # Log but don't crash the worker
                print(f"[Queue] Handler error: {e}")
            finally:
                self._processing = False
                self._queue.task_done()
    
    @property
    def size(self) -> int:
        """Current queue size."""
        return self._queue.qsize()
    
    @property
    def is_processing(self) -> bool:
        """Whether the worker is currently processing a message."""
        return self._processing
    
    @property
    def is_running(self) -> bool:
        """Whether the worker is running."""
        return self._running
    
    def stats(self) -> dict:
        """Get queue statistics."""
        return {
            "queued": self.size,
            "processed": self.processed_count,
            "dropped": self.dropped_count,
            "is_processing": self._processing,
            "is_running": self._running,
        }


# Global singleton
message_queue = MessageQueue()
