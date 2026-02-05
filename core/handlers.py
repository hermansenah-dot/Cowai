"""Discord event handlers for Cowai bot (Maicé).
All Discord event logic is moved here from bot.py as per the refactor plan.
"""

# Import necessary modules (discord, commands, trust, etc.)
import discord
import asyncio
from ai import ask_llama
from core.mood import trust
from commands_main import handle_commands
from utils.logging import log

# Add more imports as needed for event logic


async def on_message(message: discord.Message, client: discord.Client, store, DEFAULT_TZ, Long_Term_Memory, enqueue_burst_message, ALLOWED_CHANNEL_IDS):
    """
    Main message handler.
    Order:
    1) Ignore self / empty messages
    2) Enforce allowed channels (return early)
    3) Route commands to commands.py
    4) Otherwise: queue for AI chat
    """
    if message.author == client.user:
        return
    content = (message.content or "").strip()
    if not content:
        return
    if message.channel.id not in ALLOWED_CHANNEL_IDS:
        return
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
    user_text = content.replace(f"<@{client.user.id}>", "").strip()
    if not user_text:
        return
    try:
        current_trust = trust.get_score(message.author.id)
        if current_trust == 0.0:
            return  # Ignore user completely
        if current_trust < 0.7:
            trust.add(message.author.id, 0.02, reason="active chatter")
    except Exception as e:
        log(f"[Trust] Could not update trust for {message.author.id}: {e}")
    if not hasattr(client, "_last_messages"):
        client._last_messages = {}
    import time
    now = time.time()
    user_id = message.author.id
    last_msg, last_time = client._last_messages.get(user_id, (None, 0))
    if user_text == last_msg or (now - last_time) < 1.5:
        return
    client._last_messages[user_id] = (user_text, now)
    await enqueue_burst_message(message, user_text)

# Add other event handlers as needed (on_ready, on_member_join, etc.)
