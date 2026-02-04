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
    """Join voice, synthesize audio, play it, disconnect."""

    if not message.author.voice or not message.author.voice.channel:
        await message.channel.send("You need to be in a voice channel first.")
        return

    voice_channel = message.author.voice.channel
    guild = message.guild

    audio_path = TEMP_DIR / f"{uuid.uuid4().hex}.mp3"

    try:
        spoken_text = normalize_text(text)
        if not spoken_text:
            return

        # 1) Generate audio with Edge TTS
        async with _tts_lock:
            await _synthesize_to_file(spoken_text, audio_path)

        # 2) Connect or move bot
        vc = guild.voice_client
        if vc and vc.is_connected():
            if vc.channel.id != voice_channel.id:
                await vc.move_to(voice_channel)
        else:
            vc = await voice_channel.connect()

        # 3) Play via FFmpeg
        ffmpeg_exe = _find_ffmpeg_exe()
        audio = discord.FFmpegPCMAudio(str(audio_path), executable=ffmpeg_exe)

        done = asyncio.Event()

        def _after(err):
            done.set()

        vc.play(audio, after=_after)
        await done.wait()
        await vc.disconnect()

    except Exception as e:
        print(traceback.format_exc())
        await message.channel.send(f"TTS error: `{e}`")
        try:
            if guild and guild.voice_client:
                await guild.voice_client.disconnect()
        except Exception:
            pass

    finally:
        try:
            if audio_path.exists():
                audio_path.unlink()
        except Exception:
            pass


async def handle_tts_lines(message: discord.Message, lines: list[str]) -> None:
    """Join voice once, speak multiple lines back-to-back, then disconnect."""
    lines = [normalize_text(x) for x in (lines or [])]
    lines = [x for x in lines if x]
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

        try:
            await vc.disconnect()
        except Exception:
            pass

    except Exception as e:
        print(traceback.format_exc())
        await message.channel.send(f"TTS error: `{e}`")
        try:
            if guild.voice_client:
                await guild.voice_client.disconnect()
        except Exception:
            pass
