"""
Edge-TTS voice synthesis module.

Replaces Coqui TTS with Microsoft Edge TTS (cloud-based, no GPU needed).
Provides the same interface: handle_tts_command, handle_tts_lines, warmup_tts.
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
import os
import shutil
import traceback

import discord
import edge_tts


# =========================
# Configuration
# =========================

TEMP_DIR = Path("tts_tmp")
TEMP_DIR.mkdir(exist_ok=True)

# Edge TTS voice - see full list with: edge-tts --list-voices
# Good options: en-US-JennyNeural, en-US-AriaNeural, en-GB-SoniaNeural
VOICE = "en-US-JennyNeural"

# Voice tuning
RATE = "-8%"      # Speed: -50% to +100% (negative = slower)
VOLUME = "-6%"    # Volume: -50% to +100%
PITCH = "+40Hz"   # Pitch adjustment


def _find_ffmpeg_exe() -> str | None:
    """Return a path to an FFmpeg exe that can find its shared DLLs on Windows."""
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        pkgs_root = Path(localappdata) / "Microsoft" / "WinGet" / "Packages"
        if pkgs_root.is_dir():
            for pkg in pkgs_root.glob("Gyan.FFmpeg.Shared_*"):
                for exe in pkg.glob("ffmpeg-*-full_build-shared/bin/ffmpeg.exe"):
                    if exe.is_file():
                        return str(exe)

    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")


# Serialize synthesis calls to avoid concurrency issues
_tts_lock = asyncio.Lock()


def normalize_text(text: str) -> str:
    """Minimal text normalization for natural speech."""
    return (text or "").strip()


async def _synthesize_to_file(text: str, output_path: Path) -> None:
    """Generate audio file using Edge TTS."""
    communicate = edge_tts.Communicate(
        text,
        voice=VOICE,
        rate=RATE,
        volume=VOLUME,
        pitch=PITCH,
    )
    await communicate.save(str(output_path))


async def warmup_tts() -> None:
    """Warm up Edge TTS (optional - Edge TTS is already fast)."""
    # Edge TTS doesn't need warmup like local models, but we keep the interface
    pass


# =========================
# Core logic
# =========================

async def handle_tts_command(message: discord.Message, text: str) -> None:
    """Synthesize audio and play it in the bot's current voice channel (no auto-disconnect)."""

    guild = message.guild
    if not guild:
        return

    # Check if bot is already in a voice channel
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        await message.channel.send("I'm not in a voice channel. Use `!join` first.")
        return

    audio_path = TEMP_DIR / f"{uuid.uuid4().hex}.mp3"

    try:
        spoken_text = normalize_text(text)
        if not spoken_text:
            return

        # 1) Generate audio with Edge TTS
        async with _tts_lock:
            await _synthesize_to_file(spoken_text, audio_path)

        # 2) Play via FFmpeg (bot is already connected)
        ffmpeg_exe = _find_ffmpeg_exe()
        audio = discord.FFmpegPCMAudio(str(audio_path), executable=ffmpeg_exe)

        done = asyncio.Event()

        def _after(err):
            done.set()

        vc.play(audio, after=_after)
        await done.wait()
        # NOTE: No disconnect - bot stays in channel

    except Exception as e:
        print(traceback.format_exc())
        await message.channel.send(f"TTS error: `{e}`")

    finally:
        try:
            if audio_path.exists():
                audio_path.unlink()
        except Exception:
            pass


async def handle_tts_lines(message: discord.Message, lines: list[str]) -> None:
    """Speak multiple lines back-to-back in the bot's current voice channel (no auto-disconnect)."""
    lines = [normalize_text(x) for x in (lines or [])]
    lines = [x for x in lines if x]
    if not lines:
        return

    guild = message.guild
    if guild is None:
        return

    # Check if bot is already in a voice channel
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        # For auto-voice replies, silently return if not in channel
        return

    try:
        ffmpeg_exe = _find_ffmpeg_exe()

        for spoken_text in lines:
            audio_path = TEMP_DIR / f"{uuid.uuid4().hex}.mp3"
            try:
                # Synthesize
                async with _tts_lock:
                    await _synthesize_to_file(spoken_text, audio_path)

                # Play
                audio = discord.FFmpegPCMAudio(str(audio_path), executable=ffmpeg_exe)

                done = asyncio.Event()

                def _after(err):
                    done.set()

                vc.play(audio, after=_after)
                await done.wait()

            finally:
                try:
                    if audio_path.exists():
                        audio_path.unlink()
                except Exception:
                    pass

        # NOTE: No disconnect - bot stays in channel

    except Exception as e:
        print(traceback.format_exc())
        await message.channel.send(f"TTS error: `{e}`")
