"""Voice receive and STT logic for Cowai bot (Maicé)."""

import asyncio
import io
import time
import wave
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

import discord
from discord.ext import voice_recv

from utils.logging import log

# -------------------------
# Configuration
# -------------------------
SILENCE_THRESHOLD = 0.6
MIN_AUDIO_DURATION = 0.3
MAX_AUDIO_DURATION = 15.0
AUDIO_TMP_DIR = Path("stt_tmp")

# -------------------------
# Audio Buffer
# -------------------------
class UserAudioBuffer:
	def __init__(self, user_id: int):
		self.user_id = user_id
		self.audio_data: list[bytes] = []
		self.last_packet_time: float = 0.0
		self.start_time: float = 0.0

# ... (rest of voice_listen.py logic here, including STT integration) ...

# -------------------------
# STT (Whisper.cpp via pywhispercpp)
# -------------------------
import os
from pywhispercpp.model import Model

DEFAULT_MODEL = "base"
MODEL_DIR = os.environ.get("WHISPER_MODEL_DIR", None)
DEFAULT_LANGUAGE = "en"
_model: Optional[Model] = None

def _get_model() -> Model:
	global _model
	if _model is None:
		log(f"[STT] Loading whisper.cpp model: {DEFAULT_MODEL}")
		_model = Model(DEFAULT_MODEL, models_dir=MODEL_DIR)
		log("[STT] Whisper model loaded.")
	return _model

def warmup() -> bool:
	try:
		_get_model()
		return True
	except Exception as e:
		log(f"[STT] Failed to load Whisper model: {e}")
		return False

def transcribe(audio_path: str) -> str:
	model = _get_model()
	result = model.transcribe(audio_path, language=DEFAULT_LANGUAGE)
	return result['text'] if isinstance(result, dict) and 'text' in result else ""
