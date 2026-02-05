# Refactor Plan for Discord AI Bot (Maicé)

## Goals
- Improve maintainability, modularity, and clarity
- Reduce technical debt and simplify code paths
- Prepare for future features (streaming STT, advanced memory, persona variants)
- Ensure all modules, classes, and functions are properly commented and documented for clarity and maintainability
- Enforce trust-based ignore (users with trust=0.0 are ignored)
- Add basic spam filter (ignore repeated/rapid messages)
- Context window is now 5 messages

## 1. Core Structure Refactor
- Move all Discord event logic from bot.py into core/handlers.py
- Split core/conversation.py into:
  - core/conversation.py (AI logic)
  - core/context.py (Discord context)
  - core/mood.py (emotion, trust, persona)
- Create core/stt.py for all speech-to-text logic

## 2. Memory System
- Merge memory_sqlite.py and memory_vector.py into memory/
  - memory/db.py (SQLite logic)
  - memory/vector.py (embedding, similarity)
  - memory/episodes.py (episode extraction)
- Move persona memory files to personality/memory/
- Standardize memory API for retrieval, update, and embedding

## 3. Voice & STT
- Move all voice receive and TTS logic to voice/
  - voice/listen.py (voice receive, STT)
  - voice/tts.py (TTS, Coqui)
  - voice/utils.py (audio conversion)
- Refactor stt_whisper.py to support streaming and partial results

## 4. Commands & Routing
- Split commands.py into:
  - commands/core.py (main commands)
  - commands/admin.py (admin-only commands)
  - commands/voice.py (voice/TTS commands)
- Move command registration to a single router

## 5. Utilities & Logging
- Move all helpers to utils/
  - utils/helpers.py (time, clamp, etc.)
  - utils/logging.py (logging)
  - utils/text.py (WordFilter, split_for_discord)
  - utils/burst.py (BurstBuffer)
- Add utils/errors.py for error handling and reporting

## 6. Persona & Emotion
- Refactor personality/persona.py for easier persona switching
- Move emotion.py and trust.py to core/mood.py
- Standardize persona API for emotion, trust, and style

## 7. Configuration & Setup
- Move config.py to config/
  - config/core.py (Discord, AI)
  - config/voice.py (voice, TTS)
  - config/memory.py (memory settings)
- Add config/README.md for setup instructions

## 8. Tests & Validation
- Move all tests to tests/
  - tests/unit/ (unit tests)
  - tests/integration/ (integration tests)
- Add tests for memory, STT, persona, and command routing

## 9. Documentation
- Add README.md for each major folder
- Add API docs for memory, persona, and voice modules
- Add developer onboarding guide

## 10. Future Improvements
- Streaming STT and real-time transcription
- Persona variants and multi-character support
- Advanced memory search and episode linking
- Modular plugin system for commands and features

---
This plan is based on the current workspace structure and codebase. Adjust as needed for new features or architectural changes.