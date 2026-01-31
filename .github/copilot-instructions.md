# Cowai (Discord AI Bot) - Copilot Instructions

## Architecture Overview

This is a **Discord AI chatbot** ("Maicé") that uses a local Ollama LLM. Key data flows:

```
Discord message → bot.py → burst buffer → ai.py (Ollama) → response filtering → Discord reply
                    ↓                         ↑
              commands.py              personality/persona.py
                    ↓                         ↑
         memory_sqlite.py ←→ emotion.py ←→ trust.py
```

### Core Components
- **bot.py**: Entry point, Discord event handlers, channel gating (`ALLOWED_CHANNEL_IDS`)
- **ai.py**: Ollama `/api/chat` wrapper with telemetry sanitization and special token cleanup
- **commands.py**: Single source of truth for `!` commands (`!reminder`, `!tts`, `!voice`, `!trust`, `!uptime`)
- **utils/**: Shared utilities package
  - `logging.py`: Timestamped console/file logging
  - `text.py`: `WordFilter`, `split_for_discord()`, `chunk_text_for_tts()`
  - `burst.py`: `BurstBuffer` for multi-message buffering
- **personality/**: Persona definition (`persona.py`), short-term context (`memory_short.py`), long-term memory (`memory_long.py`)
- **emotion.py**: Global VAD affect engine (valence/arousal/dominance) with time-based decay
- **trust.py**: Per-user trust scores in SQLite, influences mood sensitivity and tone relaxation
- **memory_sqlite.py**: SQLite storage for facts, episodes, and message logs
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
├── memory.db      # SQLite: facts, episodes, message log (all users)
├── trust.db       # SQLite: per-user trust scores + event log
└── users/         # JSON snapshots (debugging/compatibility only)
```
Long-term memory extraction runs in background threads via `maybe_extract()`.

## Key Conventions

### Command Handling
- All commands start with `!` and route through `handle_commands()` in commands.py
- Commands **must not** create reminders implicitly; only `!reminder` creates reminders
- Admin-only commands check via `_is_admin()` (requires guild `administrator` permission)

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

The bot persona is defined in `personality/persona.py` (character: "Maicé"). When modifying:
- Keep responses short and conversational
- Emotion state affects tone via `persona_with_emotion()`
- Safety addendum is appended in `ai.py:build_system_prompt()`
