"""voice_listen.py

Discord voice receive handler for STT integration using discord-ext-voice-recv.

When the bot joins a voice channel via !join, this module:
1. Captures audio from users speaking
2. Buffers audio until user stops talking
3. Transcribes via whisper.cpp
4. Routes transcription to AI conversation handler
"""

from __future__ import annotations

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

if TYPE_CHECKING:
    pass


# -------------------------
# Configuration
# -------------------------

# Silence threshold (seconds) before processing buffered audio
SILENCE_THRESHOLD = 0.6

# Minimum audio duration to process (skip very short sounds)
MIN_AUDIO_DURATION = 0.3

# Maximum audio duration (prevent very long recordings)
MAX_AUDIO_DURATION = 15.0

# Temp directory for audio files
AUDIO_TMP_DIR = Path("stt_tmp")


# -------------------------
# Audio Buffer
# -------------------------

class UserAudioBuffer:
    """Buffer audio data for a single user."""
    
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.audio_data: list[bytes] = []
        self.last_packet_time: float = 0.0
        self.start_time: float = 0.0
    
    def add_packet(self, data: bytes) -> None:
        """Add audio packet to buffer."""
        now = time.time()
        if not self.audio_data:
            self.start_time = now
        self.audio_data.append(data)
        self.last_packet_time = now
    
    def duration(self) -> float:
        """Estimate audio duration in seconds."""
        if not self.audio_data:
            return 0.0
        # Discord sends 48kHz, 16-bit stereo = 192000 bytes/sec
        total_bytes = sum(len(d) for d in self.audio_data)
        return total_bytes / 192000.0
    
    def time_since_last_packet(self) -> float:
        """Seconds since last audio packet."""
        if self.last_packet_time == 0:
            return float("inf")
        return time.time() - self.last_packet_time
    
    def get_wav_bytes(self) -> bytes:
        """Convert buffered PCM to WAV format at 16kHz mono (whisper.cpp requirement)."""
        if not self.audio_data:
            return b""
        
        pcm = b"".join(self.audio_data)
        
        # Discord audio: 48kHz, 16-bit, stereo
        # Whisper needs: 16kHz, 16-bit, mono
        
        import array
        
        # Convert bytes to array of 16-bit samples
        samples = array.array('h', pcm)
        
        # Convert stereo to mono (average left+right channels)
        mono_samples = []
        for i in range(0, len(samples) - 1, 2):
            mono_samples.append((samples[i] + samples[i + 1]) // 2)
        
        # Downsample from 48kHz to 16kHz (take every 3rd sample)
        resampled = mono_samples[::3]
        
        # Convert back to bytes
        output = array.array('h', resampled)
        
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setnchannels(1)      # Mono
            wav.setsampwidth(2)      # 16-bit
            wav.setframerate(16000)  # 16kHz for whisper
            wav.writeframes(output.tobytes())
        
        return buffer.getvalue()
    
    def clear(self) -> None:
        """Clear the buffer."""
        self.audio_data.clear()
        self.last_packet_time = 0.0
        self.start_time = 0.0


# -------------------------
# STT Sink
# -------------------------

class STTSink(voice_recv.AudioSink):
    """
    Audio sink that captures voice and triggers STT.
    Inherits from AudioSink (base class) instead of BasicSink.
    """
    
    def __init__(
        self,
        text_channel: discord.TextChannel,
        on_transcription: Callable[[discord.Member, str], asyncio.Future],
    ):
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
        """We want decoded PCM, not opus."""
        return False
    
    def write(self, user: discord.User | discord.Member, data: voice_recv.VoiceData) -> None:
        """Called for each audio packet received. Handles OpusError gracefully."""
        try:
            if user is None or user.bot:
                return
            user_id = user.id
            if user_id not in self.buffers:
                self.buffers[user_id] = UserAudioBuffer(user_id)
            self.buffers[user_id].add_packet(data.pcm)
        except Exception as e:
            import discord
            if isinstance(e, getattr(discord.opus, 'OpusError', Exception)):
                log(f"[STT] OpusError (corrupted stream) ignored.")
            else:
                log(f"[STT] Error in write: {e}")
    
    def cleanup(self) -> None:
        """Called when sink is disconnected."""
        self._running = False
        if hasattr(self, '_cleanup_task') and self._cleanup_task:
            self._cleanup_task.cancel()
        if hasattr(self, 'buffers'):
            self.buffers.clear()
    
    def start_processing(self, loop: asyncio.AbstractEventLoop) -> None:
        """Start background task to check for silence and process audio."""
        self._loop = loop
        self._running = True
        self._cleanup_task = loop.create_task(self._process_loop())
    
    async def _process_loop(self) -> None:
        """Check buffers for silence and trigger STT."""
        while self._running:
            await asyncio.sleep(0.1)  # Check every 100ms for faster response
            
            users_to_process: list[int] = []
            
            for user_id, buf in list(self.buffers.items()):
                if not buf.audio_data:
                    continue
                
                silence_time = buf.time_since_last_packet()
                duration = buf.duration()
                
                # Process if silence detected or max duration reached
                if silence_time >= SILENCE_THRESHOLD or duration >= MAX_AUDIO_DURATION:
                    if duration >= MIN_AUDIO_DURATION:
                        users_to_process.append(user_id)
                    else:
                        buf.clear()  # Too short, discard
            
            for user_id in users_to_process:
                await self._process_user_audio(user_id)
    
    async def _process_user_audio(self, user_id: int) -> None:
        """Process buffered audio for a user in a thread pool."""
        buf = self.buffers.get(user_id)
        if not buf or not buf.audio_data:
            return
        # Get member from guild
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
            from stt_whisper import transcribe
            loop = asyncio.get_event_loop()
            # Submit transcription to thread pool
            text = await loop.run_in_executor(self._executor, transcribe, str(audio_path))
            if text and text.strip():
                # Only send to Discord/AI if not [BLANK_AUDIO]
                if text.strip() == '[BLANK_AUDIO]':
                    log(f"[STT] {member.display_name} ({member.id}): [BLANK_AUDIO] (not sent to Discord)")
                else:
                    log(f"[STT] {member.display_name} ({member.id}): {text}")
                    await self.on_transcription(member, text.strip())
            else:
                log(f"[STT] No speech detected from {member.display_name}")
        except Exception as e:
            log(f"[STT] Error processing audio: {e}")
            buf.clear()
        finally:
            try:
                if audio_path.exists():
                    audio_path.unlink()
            except Exception:
                pass


# -------------------------
# Voice Client Manager
# -------------------------

# Track active STT sessions per guild
_active_sinks: dict[int, STTSink] = {}


async def start_listening(
    voice_client: voice_recv.VoiceRecvClient,
    text_channel: discord.TextChannel,
    on_transcription: Callable[[discord.Member, str], asyncio.Future],
) -> bool:
    """
    Start listening for voice in a voice channel.
    
    Args:
        voice_client: Connected VoiceRecvClient
        text_channel: Where to route transcriptions
        on_transcription: Callback(member, text) for transcriptions
    
    Returns:
        True if listening started successfully
    """
    guild_id = voice_client.guild.id
    
    # Stop existing sink if any
    if guild_id in _active_sinks:
        await stop_listening(voice_client)
    
    try:
        sink = STTSink(text_channel, on_transcription)
        voice_client.listen(sink)
        
        # Start processing loop
        loop = asyncio.get_event_loop()
        sink.start_processing(loop)
        
        _active_sinks[guild_id] = sink
        log(f"[STT] Started listening in {voice_client.channel.name}")
        return True
        
    except Exception as e:
        log(f"[STT] Failed to start listening: {e}")
        return False


async def stop_listening(voice_client: voice_recv.VoiceRecvClient) -> None:
    """Stop listening for voice."""
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
    """Check if STT is active for a guild."""
    return guild_id in _active_sinks
