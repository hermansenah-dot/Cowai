## Cowai (Discord AI Bot)

A Discord bot that chats via a local Ollama model, supports per-user memory + mood drift, and includes a small command system (reminders + optional voice/TTS).

### Features
- Allowed-channel gate (only replies in `ALLOWED_CHANNEL_IDS`).
- Chat via Ollama (`ai.py` calls `http://localhost:11434/api/chat`).
- Per-user memory persisted in `memory/users/<user_id>.json`.
- Simple mood engine that drifts toward neutral (`emotion.py`).
- Banned-word filtering on AI replies; logs filtered words to `logs/filtered_words.txt`.
- Commands (single source of truth in `commands.py`):
	- `!reminder ...` (creates reminders only when explicitly requested)
	- `!tts ...` (optional Coqui TTS)
	- `!voice on/off/status` (optional auto-voice replies)

### Requirements
- Python 3.10/3.11 recommended if you want Coqui TTS.
	- The bot can still run on newer Python without TTS.
- Ollama running locally and reachable at `http://localhost:11434`.
- FFmpeg installed and on PATH if you use voice/TTS in Discord.

### Setup
1. Create a virtual environment and install dependencies:
	 - `pip install -r requirements.txt`
2. Create `config.py` (this repo ignores it to avoid leaking tokens). It must contain:
	 - `DISCORD_TOKEN = "..."`
	 - `ALLOWED_CHANNEL_IDS = {123, 456, ...}`
3. (Optional) Edit `ai.py` to set the Ollama `MODEL` you have installed.
4. (Optional) Add words to `banned_words.txt` (one word per line, lowercase).

### Run
- `python bot.py`
- `run.bat`

### Notes
- `logs/` and `memory/` are ignored by git.
- If `!tts` says Coqui failed to import, use Python 3.10/3.11 in a fresh venv.
