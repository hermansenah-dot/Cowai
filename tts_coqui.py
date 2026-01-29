"""tts_coqui.py

Local Coqui TTS plugin for Discord (ENERGETIC PRESET).

Changes vs baseline:
- Uses speaker p225 (soft teen)
- Adds energetic delivery:
  * Slightly faster tempo
  * Tiny pitch lift
  * Text energizer for punchier prosody
- Optional GPU support
- Warmup to avoid first-run lag
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import discord
from TTS.api import TTS


# =========================
# Configuration
# =========================

TEMP_DIR = Path("tts_tmp")
TEMP_DIR.mkdir(exist_ok=True)

# Multi-speaker English model
COQUI_MODEL = "tts_models/en/vctk/vits"

# Speaker choice (requested)
SPEAKER_ID = "p225"

# Audio energy tuning (safe values)
ATEMPO = 0.80          # speed (1.05–1.15 recommended)
PITCH_MULT = 0.96      # pitch (do NOT exceed ~1.05)

# GPU toggle (set True if PyTorch CUDA works)
USE_GPU = False


# Lazily-initialized global TTS engine
_tts_engine: TTS | None = None


def get_tts_engine() -> TTS:
    global _tts_engine
    if _tts_engine is None:
        _tts_engine = TTS(
            model_name=COQUI_MODEL,
            progress_bar=False,
            gpu=USE_GPU,
        )
    return _tts_engine


def energize_text(text: str) -> str:
    """Light text normalization to encourage energetic prosody."""
    text = text.strip()
    if not text:
        return text

    # Encourage punchy delivery
    text = text.replace("...", "!")
    text = text.replace("?", "??")

    # Avoid overdoing periods
    if len(text) < 150:
        text = text.replace(".", "!")

    return text


async def warmup_tts() -> None:
    """Warm the model at startup so first !tts is fast."""
    loop = asyncio.get_running_loop()

    def _warm():
        tts = get_tts_engine()
        tts.tts("hi", speaker=SPEAKER_ID)

    await loop.run_in_executor(None, _warm)


# =========================
# Core logic
# =========================

async def handle_tts_command(message: discord.Message, text: str) -> None:
    """Join voice, synthesize WAV, play it energetically, disconnect."""

    if not message.author.voice or not message.author.voice.channel:
        await message.channel.send("You need to be in a voice channel first.")
        return

    voice_channel = message.author.voice.channel
    guild = message.guild

    wav_path = TEMP_DIR / f"{uuid.uuid4().hex}.wav"

    try:
        # 1) Generate WAV with Coqui TTS
        tts = get_tts_engine()
        spoken_text = energize_text(text)

        tts.tts_to_file(
            text=spoken_text,
            speaker=SPEAKER_ID,
            file_path=str(wav_path),
        )

        # 2) Connect or move bot
        vc = guild.voice_client
        if vc and vc.is_connected():
            if vc.channel.id != voice_channel.id:
                await vc.move_to(voice_channel)
        else:
            vc = await voice_channel.connect()

        # 3) Play via FFmpeg (energy filter)
        audio = discord.FFmpegPCMAudio(
            str(wav_path),
            options=(
                f'-filter:a "'
                f'asetrate=29000*{PITCH_MULT},'
                f'atempo={1 / PITCH_MULT},'
                f'atempo={ATEMPO},'
                f'aresample=36000"'
            ),
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


async def handle_tts_lines(message: discord.Message, lines: list[str]) -> None:
    """Join voice once, speak multiple lines back-to-back, then disconnect."""
    lines = [str(x).strip() for x in (lines or []) if str(x).strip()]
    if not lines:
        return

    if not message.author.voice or not message.author.voice.channel:
        await message.channel.send("You need to be in a voice channel first.")
        return

    voice_channel = message.author.voice.channel
    guild = message.guild
    if guild is None:
        return

    vc = None
    try:
        # Connect (or move) once
        vc = guild.voice_client
        if vc and vc.is_connected():
            if vc.channel.id != voice_channel.id:
                await vc.move_to(voice_channel)
        else:
            vc = await voice_channel.connect()

        tts_engine = get_tts_engine()

        for raw in lines:
            wav_path = TEMP_DIR / f"{uuid.uuid4().hex}.wav"
            try:
                spoken_text = energize_text(raw)
                tts_engine.tts_to_file(
                    text=spoken_text,
                    speaker=SPEAKER_ID,
                    file_path=str(wav_path),
                )

                audio = discord.FFmpegPCMAudio(
                    str(wav_path),
                    options=(
                        f'-filter:a "'
                        f'asetrate=30000*{PITCH_MULT},'
                        f'atempo={1 / PITCH_MULT},'
                        f'atempo={ATEMPO},'
                        f'aresample=36000"'
                    ),
                )

                done = asyncio.Event()

                def _after(err):
                    done.set()

                vc.play(audio, after=_after)
                await done.wait()

            finally:
                try:
                    if wav_path.exists():
                        wav_path.unlink()
                except Exception:
                    pass

        try:
            await vc.disconnect()
        except Exception:
            pass

    except Exception as e:
        await message.channel.send(f"TTS error: `{e}`")
        try:
            if guild.voice_client:
                await guild.voice_client.disconnect()
        except Exception:
            pass
