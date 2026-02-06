# Unused or Legacy Files in Project

This list includes files in the project folder that are not actively used or imported by the main AI bot runtime. These files are either legacy, utility scripts, data/config, or test scripts.

## Python Scripts (not imported by the bot)
- memory_sqlite.py
- memory_vector.py
- emotion.py
- trust.py
- stt_whisper.py
- voice_listen.py
- tts_coqui.py
- voicetest.py
- test_tts_checkpoint_autoscan.py
- tools.py
- patch_finetune_config_*.py
- make_vctk_single_speaker_metadata*.py
- train_tts_with_ffmpeg_dlls*.py

## Data/Config/Model Files
- finetune_config*.json
- pretrained_model.pth
- reminders.json

## Folders (not code, runtime/data only)
- logs/
- tts_tmp/
- data/
- finetune_out/

## Added for TL;DR module
- tldr.py (actively used for webpage summarization)
- requirements.txt (updated for tldr dependencies)

> Note: Some of these files may be kept for reference, migration, or manual testing. Remove or archive as needed.
