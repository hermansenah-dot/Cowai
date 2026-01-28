"""
tts_coqui.py

Local Coqui TTS plugin for Discord.

Features:
- Triggered with !tts <text> (wired from bot.py)
- Bot joins user's voice channel
- Synthesizes speech locally with Coqui TTS
- Plays it via FFmpeg
- Disconnects

Notes:
- First run downloads the model (can take a bit)
- Uses WAV output (simple and reliable)
"""


from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import discord
from TTS.api import TTS


# =========================
# Configuration
# =========================

TEMP_DIR = Path("tts_tmp")
TEMP_DIR.mkdir(exist_ok=True)

# Pick a model (good default English single-speaker)
# You can change this later.
COQUI_MODEL = "tts_models/en/vctk/vits"

# Lazily-initialized global TTS engine
_tts_engine: TTS | None = None


def get_tts_engine() -> TTS:
    global _tts_engine
    if _tts_engine is None:
        # gpu=True if you have CUDA and want speed (optional)
        _tts_engine = TTS(model_name=COQUI_MODEL, progress_bar=False, gpu=False)
    return _tts_engine

async def warmup_tts() -> None:
    """
    Load the Coqui model and run a tiny synthesis once.
    Do this at bot startup so first !tts is fast.
    """
    loop = asyncio.get_running_loop()

    def _warm():
        tts = get_tts_engine()
        # Tiny synthesis to trigger model init
        tts.tts("hi", speaker="p225")

    await loop.run_in_executor(None, _warm)



# =========================
# Core logic
# =========================

async def handle_tts_command(message: discord.Message, text: str) -> None:
    """Join voice, synthesize a WAV, play it, disconnect."""
    if not message.author.voice or not message.author.voice.channel:
        await message.channel.send("You need to be in a voice channel first.")
        return

    voice_channel = message.author.voice.channel
    guild = message.guild

    # Output file
    wav_path = TEMP_DIR / f"{uuid.uuid4().hex}.wav"

    try:
        # 1) Generate WAV with Coqui TTS (runs locally)
        tts = get_tts_engine()
        tts.tts_to_file(
            text=text,
            speaker="p225",   # 👈 anime-ish / youthful speaker
            file_path=str(wav_path)
        )

        # 2) Connect or move bot
        vc = guild.voice_client
        if vc and vc.is_connected():
            if vc.channel.id != voice_channel.id:
                await vc.move_to(voice_channel)
        else:
            vc = await voice_channel.connect()

        # 3) Play via FFmpeg
        audio = discord.FFmpegPCMAudio(
            str(wav_path),
            options="-filter:a atempo=1.08"
        )
        done = asyncio.Event()

        def _after(err):
            done.set()

        vc.play(audio, after=_after)
        await done.wait()

        await vc.disconnect()

    except Exception as e:
        await message.channel.send(f"TTS error: `{e}`")
        try:
            if guild.voice_client:
                await guild.voice_client.disconnect()
        except Exception:
            pass

    finally:
        try:
            if wav_path.exists():
                wav_path.unlink()
        except Exception:
            pass
