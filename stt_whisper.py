
"""stt_whisper.py

Speech-to-Text using whisper.cpp via pywhispercpp.

Fast, local transcription for Discord voice input.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pywhispercpp.model import Model

from utils.logging import log


# -------------------------
# Configuration
# -------------------------

# Model sizes: tiny, base, small, medium, large
# Smaller = faster, larger = more accurate
DEFAULT_MODEL = "base"

# Models are downloaded to ~/.cache/whisper by default
# Override with WHISPER_MODEL_DIR env var
MODEL_DIR = os.environ.get("WHISPER_MODEL_DIR", None)

# Language hint (None = auto-detect)
DEFAULT_LANGUAGE = "en"


# -------------------------
# Lazy model loading
# -------------------------

_model: Optional[Model] = None


def _get_model() -> Model:
    """Lazy-load the Whisper model."""
    global _model
    if _model is None:
        log(f"[STT] Loading whisper.cpp model: {DEFAULT_MODEL}")
        _model = Model(DEFAULT_MODEL, models_dir=MODEL_DIR)
        log("[STT] Whisper model loaded.")
    return _model


def warmup() -> bool:
    """Pre-load the model. Returns True if successful."""
    try:
        _get_model()
        return True
    except Exception as e:
        log(f"[STT] Failed to load Whisper model: {e}")
        return False


# -------------------------
# Transcription
# -------------------------

def transcribe(
    audio_path: str | Path,
    language: str = DEFAULT_LANGUAGE,
    user_id: int = None,
) -> str:
    """
    Transcribe an audio file to text.
    
    Args:
        audio_path: Path to audio file (WAV, MP3, etc.)
        language: Language code (e.g., "en", "es", "ja") or None for auto-detect
    
    Returns:
        Transcribed text (empty string on failure)
    """
    audio_path = Path(audio_path)
    
    if not audio_path.exists():
        log(f"[STT] Audio file not found: {audio_path}")
        return ""
    
    try:
        model = _get_model()
        
        # Transcribe
        segments = model.transcribe(
            str(audio_path),
            language=language,
        )
        
        # Combine all segments
        text = " ".join(seg.text.strip() for seg in segments if seg.text)
            # Split into sentences (simple split on . ! ?)
        import re
        sentences = re.split(r'(?<=[.!?])\s+', text)
        for sentence in sentences:
            s = sentence.strip()
            if s and s != '[BLANK_AUDIO]':
                if user_id is not None:
                    print(f"[{user_id}] {s}")
                else:
                    print(s)
        
        return text.strip()
        
    except Exception as e:
        log(f"[STT] Transcription failed: {e}")
        return ""


def transcribe_with_timing(
    audio_path: str | Path,
    language: str = DEFAULT_LANGUAGE,
) -> list[dict]:
    """
    Transcribe audio with word-level timing.
    
    Returns:
        List of segments: [{"start": float, "end": float, "text": str}, ...]
    """
    audio_path = Path(audio_path)
    
    if not audio_path.exists():
        return []
    
    try:
        model = _get_model()
        segments = model.transcribe(str(audio_path), language=language)
        
        return [
            {
                "start": seg.t0 / 100.0,  # Convert to seconds
                "end": seg.t1 / 100.0,
                "text": seg.text.strip(),
            }
            for seg in segments
            if seg.text
        ]
        
    except Exception as e:
        log(f"[STT] Transcription failed: {e}")
        return []


# -------------------------
# Utilities
# -------------------------

def is_available() -> bool:
    """Check if whisper.cpp is available and working."""
    try:
        _get_model()
        return True
    except Exception:
        return False


def set_model(model_name: str) -> None:
    """
    Change the Whisper model.
    
    Args:
        model_name: One of: tiny, base, small, medium, large
    """
    global _model, DEFAULT_MODEL
    DEFAULT_MODEL = model_name
    _model = None  # Force reload on next use
    log(f"[STT] Model changed to: {model_name}")


def unload_model() -> None:
    """Unload the model from memory."""
    global _model
    _model = None
    log("[STT] Model unloaded.")
