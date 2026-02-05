"""
Speech-to-text (STT) logic for Cowai bot (Maicé).
Unifies whisper.cpp-based transcription and Discord voice receive integration.

Provides:
- STT model loading and management (whisper.cpp via pywhispercpp)
- Audio buffer and conversion utilities
- Discord voice receive handler (audio sink)
- Unified API: warmup, transcribe, start_listening, stop_listening, is_listening
"""

import os
import io
import math
import time
import wave
import array
import asyncio
from pathlib import Path
from typing import Optional, Callable, Any
import discord
from utils.logging import log
from utils.errors import log_error

# -------------------------
# Whisper.cpp Model (pywhispercpp)
# -------------------------
try:
	from pywhispercpp.model import Model as WhisperModel
except ImportError:
	WhisperModel = None

DEFAULT_MODEL = "small"
MODEL_DIR = os.environ.get("WHISPER_MODEL_DIR", None)
DEFAULT_LANGUAGE = "en"
_model: Optional[Any] = None

def _get_model() -> Any:
	global _model
	if _model is None and WhisperModel is not None:
		try:
			log(f"[STT] Loading whisper.cpp model: {DEFAULT_MODEL}")
			_model = WhisperModel(DEFAULT_MODEL, models_dir=MODEL_DIR)
			log("[STT] Whisper model loaded.")
		except Exception as exc:
			log_error("Failed to load Whisper model.", exc)
	return _model

def warmup() -> bool:
	try:
		_get_model()
		return True
	except Exception as exc:
		log_error("[STT] Failed to load Whisper model.", exc)
		return False

def transcribe(audio_path: str | Path, language: str = DEFAULT_LANGUAGE, user_id: int = None) -> str:
	audio_path = Path(audio_path)
	if not audio_path.exists():
		log_error(f"[STT] Audio file not found: {audio_path}")
		return ""
	try:
		model = _get_model()
		segments = model.transcribe(str(audio_path), language=language)
		text = " ".join(seg.text.strip() for seg in segments if seg.text)
		return text.strip()
	except Exception as exc:
		log_error("[STT] Transcription failed.", exc)
		return ""

# Async version of transcribe using asyncio.to_thread
async def transcribe_async(audio_path: str | Path, language: str = DEFAULT_LANGUAGE, user_id: int = None) -> str:
    return await asyncio.to_thread(transcribe, audio_path, language, user_id)

def is_available() -> bool:
	try:
		_get_model()
		return True
	except Exception:
		return False

def set_model(model_name: str) -> None:
	global _model, DEFAULT_MODEL
	DEFAULT_MODEL = model_name
	_model = None
	log(f"[STT] Model changed to: {model_name}")

def unload_model() -> None:
	global _model
	_model = None
	log("[STT] Model unloaded.")

# -------------------------
# Audio Buffer for Discord Voice
# -------------------------
class UserAudioBuffer:
	def __init__(self, user_id: int):
		self.user_id = user_id
		self.audio_data: list[bytes] = []
		self.last_packet_time: float = 0.0
		self.start_time: float = 0.0
	def add_packet(self, data: bytes) -> None:
		now = time.time()
		if not self.audio_data:
			self.start_time = now
		self.audio_data.append(data)
		self.last_packet_time = now
	def duration(self) -> float:
		if not self.audio_data:
			return 0.0
		total_bytes = sum(len(d) for d in self.audio_data)
		return total_bytes / 192000.0
	def time_since_last_packet(self) -> float:
		if self.last_packet_time == 0:
			return float("inf")
		return time.time() - self.last_packet_time
	def get_wav_bytes(self) -> bytes:
		if not self.audio_data:
			return b""
		pcm = b"".join(self.audio_data)
		samples = array.array('h', pcm)
		mono_samples = [(samples[i] + samples[i + 1]) // 2 for i in range(0, len(samples) - 1, 2)]
		resampled = mono_samples[::3]
		output = array.array('h', resampled)
		buffer = io.BytesIO()
		with wave.open(buffer, "wb") as wav:
			wav.setnchannels(1)
			wav.setsampwidth(2)
			wav.setframerate(16000)
			wav.writeframes(output.tobytes())
		return buffer.getvalue()
	def clear(self) -> None:
		self.audio_data.clear()
		self.last_packet_time = 0.0
		self.start_time = 0.0

# -------------------------
# Discord Voice Receive Handler
# -------------------------
from discord.ext import voice_recv
SILENCE_THRESHOLD = 1.5
MIN_AUDIO_DURATION = 0.3
MAX_AUDIO_DURATION = 15.0
AUDIO_TMP_DIR = Path("stt_tmp")

class STTSink(voice_recv.AudioSink):
	def __init__(self, text_channel: discord.TextChannel, on_transcription: Callable[[discord.Member, str], asyncio.Future]):
		super().__init__()
		self.text_channel = text_channel
		self.on_transcription = on_transcription
		self.buffers: dict[int, UserAudioBuffer] = {}
		self._processing_lock = asyncio.Lock()
		self._loop: Optional[asyncio.AbstractEventLoop] = None
		self._cleanup_task: Optional[asyncio.Task] = None
		self._running = False
		import concurrent.futures
		self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
	def wants_opus(self) -> bool:
		return False
	def write(self, user: discord.User | discord.Member, data: voice_recv.VoiceData) -> None:
		try:
			if user is None or user.bot:
				return
			user_id = user.id
			if user_id not in self.buffers:
				self.buffers[user_id] = UserAudioBuffer(user_id)
			self.buffers[user_id].add_packet(data.pcm)
		except Exception as exc:
			import discord
			if isinstance(exc, getattr(discord.opus, 'OpusError', Exception)):
				log_error("[STT] OpusError (corrupted stream) ignored.", exc)
			else:
				log_error("[STT] Error in write.", exc)
	def cleanup(self) -> None:
		self._running = False
		if hasattr(self, '_cleanup_task') and self._cleanup_task:
			try:
				self._cleanup_task.cancel()
			except Exception as exc:
				log_error("[STT] Cleanup task cancel failed.", exc)
		if hasattr(self, 'buffers'):
			self.buffers.clear()
	def start_processing(self, loop: asyncio.AbstractEventLoop) -> None:
		self._loop = loop
		self._running = True
		self._cleanup_task = loop.create_task(self._process_loop())
	async def _process_loop(self) -> None:
		while self._running:
			await asyncio.sleep(0.1)
			users_to_process: list[int] = []
			for user_id, buf in list(self.buffers.items()):
				if not buf.audio_data:
					continue
				silence_time = buf.time_since_last_packet()
				duration = buf.duration()
				if silence_time >= SILENCE_THRESHOLD or duration >= MAX_AUDIO_DURATION:
					if duration >= MIN_AUDIO_DURATION:
						users_to_process.append(user_id)
					else:
						buf.clear()
			for user_id in users_to_process:
				await self._process_user_audio(user_id)
	async def _process_user_audio(self, user_id: int) -> None:
		buf = self.buffers.get(user_id)
		if not buf or not buf.audio_data:
			return
		guild = self.text_channel.guild
		member = guild.get_member(user_id)
		if not member:
			buf.clear()
			return
		duration = buf.duration()
		log(f"[STT] Processing {duration:.1f}s audio from {member.display_name}")
		AUDIO_TMP_DIR.mkdir(exist_ok=True)
		audio_path = AUDIO_TMP_DIR / f"stt_{user_id}_{int(time.time())}.wav"
		try:
			wav_data = buf.get_wav_bytes()
			audio_path.write_bytes(wav_data)
			buf.clear()
			text = await transcribe_async(str(audio_path))
			if text and text.strip() and text.strip() != '[BLANK_AUDIO]' and text.strip().upper() != '[INAUDIBLE]':
				log(f"[STT] {member.display_name} ({member.id}): {text}")
				await self.on_transcription(member, text.strip())
			else:
				log_error(f"[STT] No speech detected from {member.display_name}")
		except Exception as exc:
			log_error("[STT] Error processing audio.", exc)
			buf.clear()
		finally:
			try:
				if audio_path.exists():
					audio_path.unlink()
			except Exception as exc:
				log_error("[STT] Failed to delete temp audio file.", exc)

# Track active STT sessions per guild
_active_sinks: dict[int, STTSink] = {}

async def start_listening(voice_client: voice_recv.VoiceRecvClient, text_channel: discord.TextChannel, on_transcription: Callable[[discord.Member, str], asyncio.Future]) -> bool:
	guild_id = voice_client.guild.id
	if guild_id in _active_sinks:
		await stop_listening(voice_client)
	try:
		sink = STTSink(text_channel, on_transcription)
		voice_client.listen(sink)
		loop = asyncio.get_event_loop()
		sink.start_processing(loop)
		_active_sinks[guild_id] = sink
		log(f"[STT] Started listening in {voice_client.channel.name}")
		return True
	except Exception as e:
		log(f"[STT] Failed to start listening: {e}")
		return False

async def stop_listening(voice_client: voice_recv.VoiceRecvClient) -> None:
	guild_id = voice_client.guild.id
	if guild_id in _active_sinks:
		try:
			voice_client.stop_listening()
		except Exception:
			pass
		sink = _active_sinks.pop(guild_id, None)
		if sink:
			sink.cleanup()
		log(f"[STT] Stopped listening in {voice_client.channel.name}")

def is_listening(guild_id: int) -> bool:
	return guild_id in _active_sinks
