

"""
Error handling and reporting utilities for Cowai bot (Maicé).

This module provides:
- Standardized error base class (`CowaiError`) for all custom exceptions
- Logging helpers for error events (`log_error`)
- Discord error reporting helper (`report_discord_error`)
- Decorator for error-wrapping async functions (`wrap_discord_errors`)

Usage Examples:
---------------

1. Raising a custom error:
	from utils.errors import CowaiError
	raise CowaiError("Something went wrong.")

2. Logging an error with traceback:
	from utils.errors import log_error
	try:
		...
	except Exception as exc:
		log_error("Failed to process event.", exc)

3. Reporting an error to Discord:
	from utils.errors import report_discord_error
	await report_discord_error(channel, "Could not complete your request.", exc)

4. Wrapping a Discord event handler:
	from utils.errors import wrap_discord_errors

	@wrap_discord_errors
	async def on_message(message):
		...

All errors are logged with timestamps and tracebacks. User-facing errors are sent to Discord channels when possible.
"""

from __future__ import annotations
import traceback
from typing import Any, Callable, Coroutine, TypeVar
from utils.logging import log

import discord

__all__ = ["CowaiError", "report_discord_error", "wrap_discord_errors"]

class CowaiError(Exception):
	"""Base exception for Cowai bot errors."""
	def __init__(self, message: str, *, cause: Exception | None = None):
		super().__init__(message)
		self.cause = cause

def log_error(message: str, exc: Exception | None = None) -> None:
	"""Log an error with traceback if available."""
	if exc:
		tb = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
		log(f"[ERROR] {message}\n{tb}")
	else:
		log(f"[ERROR] {message}")

async def report_discord_error(channel: discord.abc.Messageable, message: str, exc: Exception | None = None) -> None:
	"""Send a user-friendly error message to Discord and log details."""
	log_error(message, exc)
	try:
		await channel.send(f"❌ {message}")
	except Exception as e:
		log(f"[ERROR] Failed to send error to Discord: {e}")

F = TypeVar("F", bound=Callable[..., Coroutine[Any, Any, Any]])
def wrap_discord_errors(func: F) -> F:
	"""Decorator: catch and report errors in Discord event handlers."""
	async def wrapper(*args, **kwargs):
		try:
			return await func(*args, **kwargs)
		except Exception as exc:
			channel = None
			# Try to find a Discord channel in args
			for arg in args:
				if isinstance(arg, discord.abc.Messageable):
					channel = arg
					break
				if hasattr(arg, "channel") and isinstance(arg.channel, discord.abc.Messageable):
					channel = arg.channel
					break
			msg = f"An internal error occurred. Please try again later."
			await report_discord_error(channel, msg, exc) if channel else log_error(msg, exc)
	return wrapper  # type: ignore
