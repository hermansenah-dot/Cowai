"""TTS (text-to-speech) logic for Cowai bot (Maicé)."""

import asyncio
import uuid
from pathlib import Path
import os
import shutil
import traceback

# Coqui TTS
import torch
from TTS.api import TTS

# Edge TTS
import edge_tts as edge_tts_module

# Discord (for voice playback)
import discord

# =========================
# Configuration
# =========================

TEMP_DIR = Path("tts_tmp")
TEMP_DIR.mkdir(exist_ok=True)

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

# --- Coqui TTS config ---
COQUI_MODEL = "tts_models/en/vctk/vits"
SPEAKER_ID = "p248"  # VCTK speaker
PLAYBACK_ATEMPO = 0.90
USE_GPU = False

# --- Edge TTS config ---
EDGE_VOICE = "en-US-JennyNeural"
EDGE_RATE = "-8%"
EDGE_VOLUME = "-6%"
EDGE_PITCH = "+30Hz"

# --- Locks for concurrency ---
_coqui_tts_engine: TTS | None = None
_coqui_tts_lock = asyncio.Lock()
_edge_tts_lock = asyncio.Lock()

# =========================
# Coqui TTS Backend
# =========================

async def coqui_tts(text: str, output_path: Path) -> None:
	global _coqui_tts_engine
	async with _coqui_tts_lock:
		if _coqui_tts_engine is None:
			_coqui_tts_engine = TTS(model_name=COQUI_MODEL, progress_bar=False, gpu=USE_GPU)
		wav = _coqui_tts_engine.tts(text, speaker=SPEAKER_ID)
		_coqui_tts_engine.save_wav(wav, str(output_path))

# =========================
# Edge TTS Backend
# =========================

async def edge_tts(text: str, output_path: Path) -> None:
	async with _edge_tts_lock:
		communicate = edge_tts_module.Communicate(
			text,
			EDGE_VOICE,
			rate=EDGE_RATE,
			volume=EDGE_VOLUME,
			pitch=EDGE_PITCH,
		)
		with open(output_path, "wb") as f:
			async for chunk in communicate.stream():
				if chunk["type"] == "audio":
					f.write(chunk["data"])

# =========================
# Unified TTS Interface
# =========================

async def handle_tts_command(text: str, backend: str = "edge", output_path: Path | None = None) -> Path:
	"""Synthesize TTS using the selected backend ('edge' or 'coqui')."""
	output_path = output_path or TEMP_DIR / f"tts_{uuid.uuid4().hex}.wav"
	await edge_tts(text, output_path)
	return output_path

async def handle_tts_lines(lines: list[str], backend: str = "edge") -> list[Path]:
	"""Synthesize multiple lines to separate files."""
	paths = []
	for line in lines:
		path = await handle_tts_command(line, backend=backend)
		paths.append(path)
	return paths

async def warmup_tts():
	"""Preload TTS engines if needed."""
	# Coqui: lazy-loads on first use
	# Edge: nothing to preload
	pass
