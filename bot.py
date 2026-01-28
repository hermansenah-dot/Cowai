import discord
import time
import json
import re
import pytz
from datetime import datetime, timedelta

# ---- Internal modules ----
from ai import ask_llama
from triggers import analyze_input
from emotion import emotion
from personality.memory_short import get_short_memory
from personality.memory_long import Long_Term_Memory
from reminders import ReminderStore, reminder_loop, Reminder
from config import DISCORD_TOKEN, ALLOWED_CHANNEL_IDS


def looks_english(text: str) -> bool:
    """
    Heuristic check for English text.
    Returns False for messages that look non-English.
    """

    # Too short → ignore
    if len(text) < 2:
        return False

    # If it has a lot of non-latin letters, ignore
    non_latin = re.findall(r"[^\x00-\x7F]", text)
    if len(non_latin) > 2:
        return False

    # Common English words check
    english_hits = sum(
        1 for w in ["the", "and", "you", "is", "to", "of", "that", "it"]
        if re.search(rf"\b{w}\b", text.lower())
    )

    return english_hits >= 1

# ...existing code...
