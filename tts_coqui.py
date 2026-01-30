from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
import os
import shutil

import discord
from TTS.api import TTS


# =========================
# Configuration
# =========================

TEMP_DIR = Path("tts_tmp")
TEMP_DIR.mkdir(exist_ok=True)


def _find_ffmpeg_exe() -> str | None:
    """Return a path to an FFmpeg exe that can find its shared DLLs on Windows."""
    # Prefer WinGet FFmpeg.Shared (DLLs like avfilter-11.dll live next to ffmpeg.exe)
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        pkgs_root = Path(localappdata) / "Microsoft" / "WinGet" / "Packages"
        if pkgs_root.is_dir():
            for pkg in pkgs_root.glob("Gyan.FFmpeg.Shared_*"):
                for exe in pkg.glob("ffmpeg-*-full_build-shared/bin/ffmpeg.exe"):
                    if exe.is_file():
                        return str(exe)

    # Fallback to PATH (may be a WinGet shim; still better than nothing)
    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")

# Multi-speaker English model
COQUI_MODEL = "tts_models/en/vctk/vits"

# Speaker choice (requested)
SPEAKER_ID = "p225"

# Audio energy tuning (safe values)
ATEMPO = 0.85          # speed (1.05–1.15 recommended)
PITCH_MULT = 0.78      # pitch (do NOT exceed ~1.05)

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
        ffmpeg_exe = _find_ffmpeg_exe()
        audio = discord.FFmpegPCMAudio(
            str(wav_path),
            executable=ffmpeg_exe,
            options=(
                f'-filter:a "'
                f'asetrate=48000*{PITCH_MULT},'
                f'atempo={1 / PITCH_MULT},'
                f'atempo={ATEMPO},'
                f'aresample=48000"'
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

                ffmpeg_exe = _find_ffmpeg_exe()
                audio = discord.FFmpegPCMAudio(
                    str(wav_path),
                    executable=ffmpeg_exe,
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
