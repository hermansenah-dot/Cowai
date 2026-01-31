## Cowai (Discord AI Bot)

<img align="right" src="Maise.png" alt="Maise" width="320" />

A Discord bot that chats via a local Ollama model, supports per-user memory + mood drift, and includes a small command system (reminders + optional voice/TTS + trust).

> Note: A big part of this project is "vibe coded" — expect rough edges.

<br clear="right" />

### Features
- Allowed-channel gate (only replies in `ALLOWED_CHANNEL_IDS`).
- Chat via Ollama (`ai.py` calls `http://localhost:11434/api/chat`).
- Long-term memory (facts + episodic “memory cards”) stored in SQLite (`memory/memory.db`).
  - A small JSON snapshot is also written to `memory/users/<user_id>.json` for compatibility/debugging.
  - Only *relevant* memories are injected into the system prompt per message.
  - Periodic extraction runs in the background using strict JSON (keeps the bot responsive).
- Simple mood engine that drifts toward neutral (`emotion.py`).
- Per-user trust scores persisted in SQLite (`memory/trust.db`).
  - Used to scale how strongly messages affect mood and to provide internal “tone guidance”.
- Banned-word filtering on AI replies; logs filtered words to `logs/filtered_words.txt`.
- Commands (single source of truth in `commands.py`):
	- `!reminder ...` (creates reminders only when explicitly requested)
	- `!tts ...` (optional Coqui TTS)
	- `!voice on/off/status` (optional auto-voice replies)
	- `!uptime` (connection uptime / reconnect tracking)
	- `!trust`, `!trustwhy` (view trust)
	- `!trustset`, `!trustadd` (admin only)

### Requirements
- Python 3.11 recommended for the smoothest dependency installs.
	- Coqui TTS is optional but may be harder to install on Python 3.13 due to upstream dependency constraints.
- Ollama running locally and reachable at `http://localhost:11434`.
- FFmpeg installed and available if you use voice/TTS in Discord.

### Setup
1. Create a virtual environment and install dependencies:
	 - `pip install -r requirements.txt`
2. Create `config.py` (this repo ignores it to avoid leaking tokens). It must contain:
	 - `DISCORD_TOKEN = "..."`
	 - `ALLOWED_CHANNEL_IDS = {123, 456, ...}`
3. (Optional) Edit `ai.py` to set the Ollama `MODEL` you have installed.
4. (Optional) Add words to `banned_words.txt` (one word per line, lowercase).

#### FFmpeg (Windows)
If you use `!tts` / `!voice`, install FFmpeg. One easy option is WinGet:
- `winget install Gyan.FFmpeg.Shared`

The bot prefers a real `ffmpeg.exe` (not a shim) so FFmpeg DLLs resolve correctly.

### Run
- `python bot.py`
- `run.bat`

### Notes
- `logs/` and `memory/` are ignored by git (they are runtime data).
- Trust persistence lives in `memory/trust.db`.
- If `!tts` says Coqui failed to import, try Python 3.11 in a fresh venv.

### Memory tips
- To reset memory for everyone: delete `memory/memory.db` (and optionally `memory/users/`).
- The memory system attempts basic redaction before storing messages (tokens/keys), but you should still avoid pasting secrets in chat.
