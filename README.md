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
- **Priority message queue** based on per-user trust scores.
- Simple mood engine that drifts toward neutral (`emotion.py`).
- Per-user trust scores persisted in SQLite (`memory/trust.db`).
  - Used to scale how strongly messages affect mood and queue priority.
- Banned-word filtering on AI replies via `WordFilter`; logs filtered words to `logs/filtered_words.txt`.
- Humanization layer adds natural listening lines and conversation flow (`humanize.py`).
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

utils/              # Shared utilities
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

### Notes
- `logs/`, `memory/`, `tts_tmp/`, and `finetuning/` are ignored by git (runtime data).
- Trust persistence lives in `memory/trust.db`.
- Edge TTS requires an internet connection (uses Microsoft's cloud voices).
- Voice can be changed by editing `VOICE` in `tts_edge.py` (run `edge-tts --list-voices` to see options).

### Memory tips
- To reset memory for everyone: delete `memory/memory.db` (and optionally `memory/users/`).
- The memory system attempts basic redaction before storing messages (tokens/keys), but avoid pasting secrets in chat.
