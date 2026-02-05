## Cowai (Discord AI Bot)

<img align="right" src="Maise.png" alt="Maise" width="320" />

A Discord bot that chats via a local Ollama model, supports per-user memory + mood drift, and includes a small command system (reminders + optional voice/TTS + trust).

> Note: A big part of this project is "vibe coded" - expect rough edges. If in the future there will be personal data of any kind in the program, it will not be vibe coded for security reasons.

<br clear="right" />

### Features

- Allowed-channel gate (only replies in `ALLOWED_CHANNEL_IDS`).
- Chat via Ollama (`ai.py` calls `http://localhost:11434/api/chat`).
- Burst buffering: rapid consecutive messages from the same user are combined into a single AI request.
- **Vector-based semantic memory** using Ollama embeddings (`nomic-embed-text`):
  - Long-term memory (facts + episodic "memory cards") stored in SQLite with embeddings (`memory/memory.db`).
  - Relevant memories retrieved via cosine similarity search.
  - Periodic extraction runs in the background using strict JSON.
- **Priority message queue** based on per-user trust scores (range: 0.0–1.0; users with 0.0 are ignored).
  - Trust auto-increases for active chatters (up to 0.7), and users with 0.0 trust are ignored by the bot.
  - Basic spam protection: repeated or rapid messages are ignored.
- Simple mood engine that drifts toward neutral (`emotion.py`).
- Per-user trust scores persisted in SQLite (`memory/trust.db`).
  - Used to scale how strongly messages affect mood and queue priority.
- Banned-word filtering on AI replies via `WordFilter`; logs filtered words to `logs/filtered_words.txt`.
- Humanization layer adds natural listening lines and conversation flow (`humanize.py`).
- Context window: up to 5 recent messages included for AI context.
- Commands (single source of truth in `commands.py`):
  - `!reminder ...` (creates reminders only when explicitly requested)
  - `!join` (bot joins your voice channel and stays connected)
  - `!disconnect` / `!leave` (bot leaves voice channel)
  - `!tts ...` (Edge TTS voice synthesis, requires `!join` first)
  - `!voice on/off/status` (toggle auto-voice replies)
  - `!uptime` (connection uptime / reconnect tracking)
  - `!trust`, `!trustwhy` (view trust)
  - `!trustset`, `!trustadd` (admin only)

### Project Structure
```
bot.py              # Entry point, Discord event handlers
ai.py               # Ollama /api/chat wrapper
commands.py         # All ! commands
config.py           # Tokens & allowed channels (git-ignored)
emotion.py          # VAD affect engine
trust.py            # Per-user trust scores
humanize.py         # Conversational style layer
memory_sqlite.py    # SQLite storage for memory (with vector embeddings)
memory_vector.py    # Ollama embeddings + cosine similarity search
message_queue.py    # Priority queue for message processing
reminders.py        # Reminder system
triggers.py         # Mood delta analysis
tts_edge.py         # Edge TTS integration (Microsoft cloud voices)

core/               # Extracted bot logic
├── conversation.py # AI conversation handler, NLP hints, mood processing
├── context.py      # Discord channel context building, message sending
└── loops.py        # Background tasks (random engagement loop)

utils/              # Shared utilities
├── helpers.py      # Common functions (clamp, now_ts, get_current_time)
├── logging.py      # Timestamped logging
├── text.py         # WordFilter, split_for_discord, chunk_text_for_tts
└── burst.py        # BurstBuffer for multi-message handling

personality/        # Persona & memory
├── persona.py      # Bot character definition (Evil VTuber persona)
├── memory_short.py # Short-term context
└── memory_long.py  # Long-term memory extraction

tests/              # Unit tests
└── test_utils.py   # Tests for utils package
```

### Requirements
- Python 3.11+ (3.13 recommended).
- Ollama running locally at `http://localhost:11434`.
- FFmpeg installed if you use voice/TTS in Discord.
- Internet connection for Edge TTS (uses Microsoft cloud voices).

### Setup
1. Create a virtual environment and install dependencies:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate  # Windows
   pip install -r requirements.txt
   ```
2. Create `config.py` (git-ignored):
   ```python
   DISCORD_TOKEN = "your-token-here"
   ALLOWED_CHANNEL_IDS = {123456789, 987654321}
   ```
3. (Optional) Edit `ai.py` to set the Ollama `DEFAULT_MODEL`.
4. (Optional) Add words to `banned_words.txt` (one word per line, lowercase).

#### FFmpeg (Windows)
If you use `!tts` / `!voice`, install FFmpeg:
```bash
winget install Gyan.FFmpeg.Shared
```

### Run
```bash
python bot.py  # or run.bat
```

### Testing
```bash
python -m pytest tests/ -v
```


### Error Handling API

All error handling is standardized via `utils/errors.py`:

- Use `CowaiError` for custom exceptions.
- Use `log_error` to log errors with tracebacks.
- Use `report_discord_error` to send user-friendly error messages to Discord and log details.
- Use `wrap_discord_errors` as a decorator for async Discord event handlers to catch/report errors automatically.

**Examples:**

```python
from utils.errors import CowaiError, log_error, report_discord_error, wrap_discord_errors

# Raise a custom error
raise CowaiError("Something went wrong.")

# Log an error
try:
  ...
except Exception as exc:
  log_error("Failed to process event.", exc)

# Report an error to Discord
await report_discord_error(channel, "Could not complete your request.", exc)

# Decorate a handler
@wrap_discord_errors
async def on_message(message):
  ...
```

All errors are logged with timestamps and tracebacks. User-facing errors are sent to Discord channels when possible.
