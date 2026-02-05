
"""commands.py

All Discord command handling is now delegated to split modules.

Important design rule:
- bot.py must enforce ALLOWED_CHANNEL_IDS BEFORE calling any command handler
- therefore, every command response happens only in allowed channels
"""

from commands.core import handle_uptime, handle_reminder
from commands.admin import handle_trust_admin
from commands.voice import handle_tts, get_voice_enabled, set_voice_enabled

async def handle_commands(
    message,  # discord.Message
    content: str,
    *,
    store,  # ReminderStore
    default_tz,  # BaseTzInfo
    LongMemory,
) -> bool:
    """
    Central command router.
    Returns True if a command was handled (caller should return).
    """
    content_lower = content.lower()
    if content_lower.startswith("!uptime"):
        return await handle_uptime(message)
    if content_lower.startswith("!trustset") or content_lower.startswith("!trustadd"):
        return await handle_trust_admin(message, content)
    if content_lower.startswith("!tts"):
        return await handle_tts(message, content)
    if content_lower.startswith("!reminder"):
        return await handle_reminder(message, content, store, default_tz)
    if content_lower.startswith("!join"):
        from commands.voice import handle_join
        return await handle_join(message, content)
    # ...add more delegations for disconnect, voice, etc. as needed...
    return False
