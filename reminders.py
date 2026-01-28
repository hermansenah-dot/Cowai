
import asyncio
import json
import os
import time
from dataclasses import dataclass, asdict
from typing import List, Optional

REMINDERS_FILE = "reminders.json"

@dataclass
class Reminder:
    due_ts: float
    channel_id: int
    user_id: int
    text: str

class ReminderStore:
    def __init__(self):
        self.reminders: List[Reminder] = []
        self.load()

    def load(self):
        if os.path.exists(REMINDERS_FILE):
            with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self.reminders = [Reminder(**r) for r in raw]

    def save(self):
        with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in self.reminders], f, indent=2)

    def add(self, reminder: Reminder):
        self.reminders.append(reminder)
        self.reminders.sort(key=lambda r: r.due_ts)
        self.save()

    def pop_due(self) -> List[Reminder]:
        now = time.time()
        due, future = [], []
        for r in self.reminders:
            (due if r.due_ts <= now else future).append(r)
        self.reminders = future
        if due:
            self.save()
        return due

async def reminder_loop(bot, store: ReminderStore, poll_seconds: float = 1.0):
    while True:
        due = store.pop_due()
        for r in due:
            channel = bot.get_channel(r.channel_id)
            if channel:
                await channel.send(f"<@{r.user_id}> \u23f0 Reminder: {r.text}")
        await asyncio.sleep(poll_seconds)
