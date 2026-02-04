# Cowai (Discord AI Bot) - Copilot Instructions

## Architecture Overview

This is a **Discord AI chatbot** ("Maicé") that uses a local Ollama LLM. Key data flows:

```
Discord message → bot.py → burst buffer → core/conversation.py → ai.py (Ollama) → response filtering → Discord reply
                    ↓                              ↑
              commands.py                  personality/persona.py
                    ↓                              ↑
         memory_sqlite.py ←→ emotion.py ←→ trust.py
```

### Core Components
- **bot.py**: Entry point, Discord event handlers, channel gating (`ALLOWED_CHANNEL_IDS`), queue startup
- **core/**: Extracted bot logic
  - `conversation.py`: AI conversation handler, NLP hints, mood processing
  - `context.py`: Discord channel context building, message sending
  - `loops.py`: Background tasks (random engagement loop)
- **ai.py**: Ollama `/api/chat` wrapper with telemetry sanitization and special token cleanup
- **commands.py**: Single source of truth for `!` commands (`!reminder`, `!join`, `!disconnect`, `!tts`, `!voice`, `!trust`, `!uptime`)
- **utils/**: Shared utilities package
  - `helpers.py`: Common functions (`clamp()`, `now_ts()`, `get_current_time()`)
  - `logging.py`: Timestamped console/file logging
  - `text.py`: `WordFilter`, `split_for_discord()`, `chunk_text_for_tts()`
  - `burst.py`: `BurstBuffer` for multi-message buffering (0.3s window)
- **personality/**: Persona definition (`persona.py` - Evil VTuber style), short-term context (`memory_short.py`), long-term memory (`memory_long.py`)
- **emotion.py**: Global VAD affect engine (valence/arousal/dominance) with time-based decay
- **trust.py**: Per-user trust scores in SQLite, influences mood sensitivity and queue priority
- **memory_sqlite.py**: SQLite storage for facts, episodes, and message logs with vector embeddings
- **memory_vector.py**: Ollama embeddings (`nomic-embed-text`, 768 dims) + cosine similarity for semantic search
- **message_queue.py**: Priority queue for message processing based on trust scores
- **humanize.py**: Adds conversational listening lines and style guidance

## Critical Patterns

### Channel Restriction
All responses are gated by `ALLOWED_CHANNEL_IDS` in `config.py`. Bot.py enforces this **before** any command or AI handler runs.

### Burst Buffering
Rapid consecutive messages from the same user are buffered into a single AI request via `utils/burst.py`. The `BurstBuffer` class waits for the user to stop typing, then calls the registered handler with combined text.

### Telemetry Sanitization
`ai.py` strips UI/telemetry patterns from both input and output to prevent prompt-format lock-in. Patterns defined in `_TELEMETRY_LINE_RE`.

### Banned Word Filter
AI replies pass through `WordFilter` (in `utils/text.py`) before sending. Words defined in `banned_words.txt`, violations logged to `logs/filtered_words.txt`.

### Memory Architecture
```
memory/
├── memory.db      # SQLite: facts, episodes, message log + vector embeddings
├── trust.db       # SQLite: per-user trust scores + event log
└── users/         # JSON snapshots (debugging/compatibility only)
```
- Long-term memory extraction runs in background threads via `maybe_extract()`.
- Episodes are embedded using Ollama's `nomic-embed-text` model (768 dimensions).
- Retrieval uses cosine similarity search via `retrieve_relevant_vector()`.

### Priority Queue
Messages are processed through a priority queue (`message_queue.py`):
- Trust ≥ 0.7 → HIGH priority
- Trust ≥ 0.4 → NORMAL priority  
- Trust < 0.4 → LOW priority
- System messages → CRITICAL priority

## Key Conventions

### Command Handling
- All commands start with `!` and route through `handle_commands()` in commands.py
- Commands **must not** create reminders implicitly; only `!reminder` creates reminders
- Admin-only commands check via `_is_admin()` (requires guild `administrator` permission)
- Voice channel control: `!join` connects bot, `!disconnect` leaves. Bot stays connected until explicitly disconnected.
- `!tts` requires bot to already be in a voice channel (use `!join` first)
- `!voice on/off` only toggles auto-voice replies, doesn't join/leave channels

### Async/Threading
- Ollama calls (`ask_llama()`) are synchronous; always wrap in `asyncio.to_thread()`
- Long-term memory extraction runs in thread executors to avoid blocking Discord events
- TTS/Coqui imports are lazy-loaded to avoid slow startup

### Message Splitting
Discord's 2000-char limit is handled by `split_for_discord()`. Prefer sentence boundaries, max 5 parts.

## Configuration

**Required** (`config.py`, git-ignored):
```python
DISCORD_TOKEN = "..."
ALLOWED_CHANNEL_IDS = {123, 456}  # Set of channel IDs
```

**Ollama settings** in `ai.py`:
- `DEFAULT_MODEL`: The Ollama model reference
- `DEFAULT_NUM_PREDICT`, `DEFAULT_TEMPERATURE`, etc.

## Development

### Running
```bash
python bot.py  # or run.bat
```

### Dependencies
- Python 3.13 recommended (Coqui TTS compatibility)
- Ollama must be running at `localhost:11434`
- FFmpeg required for voice/TTS features

### Runtime Directories (git-ignored)
- `logs/`: Censorship logs, debug files
- `memory/`: SQLite databases, user JSON snapshots
- `tts_tmp/`: Temporary voice files

## Persona Guidelines

The bot persona is defined in `personality/persona.py` (character: "Maicé" - Evil VTuber style). When modifying:
- **Usually SHORT and snappy** responses, but ~20% chance of chaotic unhinged rants
- Playfully evil, mischievous, loves chaos (inspired by Evil Neuro)
- Uses lowercase, emoji spam (💀🔥✨), dramatic pauses with "..."
- Emotion state affects tone via `persona_with_emotion()`
- Safety addendum is appended in `ai.py:build_system_prompt()`
