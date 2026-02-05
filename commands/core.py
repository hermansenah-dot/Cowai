
"""Core commands for Cowai bot (Maicé)."""
import time
import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
import uptime
from ai import ask_llama
from reminders import Reminder, ReminderStore
from utils.text import chunk_text_for_tts, truncate_for_tts

def parse_in_minutes(text: str) -> Optional[int]:
	t = text.lower()
	if "remind me in" not in t:
		return None
	after = t.split("remind me in", 1)[1].strip()
	parts = after.split()
	if not parts:
		return None
	try:
		mins = int(parts[0])
	except ValueError:
		return None
	if "minute" not in after:
		return None
	return mins

def parse_at_time(text: str) -> Optional[Dict[str, Any]]:
	t = text.lower().strip()
	if "remind" not in t:
		return None
	day_offset = 1 if "tomorrow" in t else 0
	m = re.search(r"\b(at\s*)?([01]?\d|2[0-3]):([0-5]\d)\b", t)
	hour = minute = None
	if m:
		hour = int(m.group(2))
		minute = int(m.group(3))
	else:
		m2 = re.search(r"\b(at\s*)?([1-9]|1[0-2])(?::([0-5]\d))?\s*(am|pm)\b", t)
		if not m2:
			return None
		h12 = int(m2.group(2))
		minute = int(m2.group(3)) if m2.group(3) else 0
		ampm = m2.group(4)
		if ampm == "am":
			hour = 0 if h12 == 12 else h12
		else:
			hour = 12 if h12 == 12 else h12 + 12
	reminder_text = text
	if "remind me" in t:
		reminder_text = text.lower().split("remind me", 1)[1].strip()
	for token in ["tomorrow", "at", "am", "pm"]:
		reminder_text = reminder_text.replace(token, " ")
	reminder_text = " ".join(reminder_text.split()).strip(" .!-")
	if not reminder_text:
		reminder_text = "You asked me to remind you."
	return {"hour": hour, "minute": minute, "day_offset": day_offset, "text": reminder_text}

def build_due_ts_absolute(hour: int, minute: int, day_offset: int, tz) -> float:
	now = datetime.now(tz)
	target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
	if day_offset == 1:
		target = target + timedelta(days=1)
	if day_offset == 0 and target <= now:
		target = target + timedelta(days=1)
	return target.timestamp()

def llm_extract_reminder(user_text: str) -> Optional[Dict[str, Any]]:
	extraction_messages = [
		{
			"role": "system",
			"content": (
				"You are a strict JSON extraction tool. Output JSON ONLY.\n"
				"Detect whether the user wants to set a reminder.\n\n"
				"Schema:\n"
				"{\n"
				'  "intent": "set_reminder" | "none",\n'
				'  "time_type": "relative" | "absolute" | null,\n'
				'  "delay_minutes": integer | null,\n'
				'  "hour": integer | null,\n'
				'  "minute": integer | null,\n'
				'  "day_offset": integer | null,\n'
				'  "text": string | null\n'
				"}\n\n"
				"Rules:\n"
				"- If NOT a reminder request: intent='none'.\n"
				"- If time_type='relative': set delay_minutes. hour/minute/day_offset must be null.\n"
				"- If time_type='absolute': set hour (0-23), minute (0-59), day_offset (0=today, 1=tomorrow).\n"
				"- Interpret: 'half an hour'=30, 'an hour'=60, 'a couple minutes'=2, 'a few minutes'=5.\n"
				"- If user says 'tomorrow', day_offset=1.\n"
				"- text must be reminder content (short). Remove timing words.\n"
				"- If you cannot extract safely, return intent='none'.\n"
			),
		},
		{"role": "user", "content": user_text},
	]
	reply = ask_llama(extraction_messages)
	try:
		data = json.loads(reply)
	except json.JSONDecodeError:
		return None
	if not isinstance(data, dict) or data.get("intent") != "set_reminder":
		return None
	time_type = data.get("time_type")
	text = data.get("text")
	if not isinstance(text, str) or not text.strip():
		return None
	text = text.strip()
	if time_type == "relative":
		delay = data.get("delay_minutes")
		if not isinstance(delay, int) or delay <= 0 or delay > 24 * 60:
			return None
		return {"type": "relative", "delay_minutes": delay, "text": text}
	if time_type == "absolute":
		hour = data.get("hour")
		minute = data.get("minute")
		day_offset = data.get("day_offset", 0)
		if not isinstance(hour, int) or not (0 <= hour <= 23):
			return None
		if not isinstance(minute, int) or not (0 <= minute <= 59):
			return None
		if not isinstance(day_offset, int) or day_offset not in (0, 1):
			return None
		return {
			"type": "absolute",
			"hour": hour,
			"minute": minute,
			"day_offset": day_offset,
			"text": text,
		}
	return None

async def handle_uptime(message):
	if not uptime.TRACKER:
		await message.channel.send("Uptime tracker not initialized.")
		return True
	await message.channel.send(uptime.TRACKER.format_status())
	return True

async def handle_reminder(message, content, store, default_tz):
	reminder_input = content[len("!reminder"):].strip()
	if not reminder_input:
		await message.channel.send(
			"Usage:\n"
			"`!reminder remind me in 10 minutes drink water`\n"
			"`!reminder remind me tomorrow at 18:30 dinner`"
		)
		return True
	mins = parse_in_minutes(reminder_input)
	reminder_text = None
	due_ts = None
	if mins is not None:
		reminder_text = reminder_input.split("minutes", 1)[-1].strip() or "You asked me to remind you."
		due_ts = time.time() + mins * 60
	if due_ts is None:
		abs_parsed = parse_at_time(reminder_input)
		if abs_parsed:
			reminder_text = abs_parsed["text"]
			due_ts = build_due_ts_absolute(
				abs_parsed["hour"],
				abs_parsed["minute"],
				abs_parsed["day_offset"],
				default_tz,
			)
	if due_ts is None:
		extracted = await asyncio.to_thread(llm_extract_reminder, reminder_input)
		if extracted:
			reminder_text = extracted["text"]
			if extracted["type"] == "relative":
				due_ts = time.time() + extracted["delay_minutes"] * 60
			else:
				due_ts = build_due_ts_absolute(
					extracted["hour"],
					extracted["minute"],
					extracted["day_offset"],
					default_tz,
				)
	if due_ts is None or reminder_text is None:
		await message.channel.send(
			"I couldn't understand that reminder.\n"
			"Try:\n"
			"`!reminder remind me in 10 minutes stand up`\n"
			"`!reminder remind me tomorrow at 8pm check email`"
		)
		return True
	store.add(
		Reminder(
			due_ts=due_ts,
			channel_id=message.channel.id,
			user_id=message.author.id,
			text=reminder_text,
		)
	)
	time_str = datetime.fromtimestamp(due_ts, default_tz).strftime("%H:%M")
	await message.channel.send(
		f"⏰ Got it.\n"
		f"I'll remind you at **{time_str}**.\n"
		f"Message: *{reminder_text}* 😊"
	)
	return True
