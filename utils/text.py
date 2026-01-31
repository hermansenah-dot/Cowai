"""Text manipulation utilities for Discord messages."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Set

from utils.logging import log_to_file, DEFAULT_TZ
from datetime import datetime


def load_word_list(path: str | Path) -> set[str]:
    """Load a word list from a text file (one word per line).
    
    Lines starting with '#' are treated as comments.
    All words are lowercased.
    """
    words: set[str] = set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                words.add(line.lower())
    except FileNotFoundError:
        pass
    return words


class WordFilter:
    """Filter banned words from text with logging support."""
    
    def __init__(
        self,
        banned_words: set[str],
        replacement: str = "*FILTERED!*",
        log_file: str | Path | None = None,
    ):
        self.banned_words = banned_words
        self.replacement = replacement
        self.log_file = Path(log_file) if log_file else None
        
        # Pre-compile patterns for efficiency (sorted by length for greedy matching)
        self._patterns: list[tuple[str, re.Pattern[str]]] = [
            (word, re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE))
            for word in sorted(banned_words, key=len, reverse=True)
        ]
    
    def filter(self, text: str | None) -> str:
        """Filter banned words from text, returning the filtered result."""
        if not text:
            return ""
        if not self.banned_words:
            return text
        
        filtered_counts: dict[str, int] = {}
        result = text
        
        for word, pattern in self._patterns:
            result, count = pattern.subn(self.replacement, result)
            if count:
                filtered_counts[word] = filtered_counts.get(word, 0) + count
        
        if filtered_counts and self.log_file:
            self._log_censorship(filtered_counts)
        
        return result
    
    def _log_censorship(self, filtered_counts: dict[str, int]) -> None:
        """Log filtered words to file."""
        items = ", ".join(f"{w}(x{n})" for w, n in sorted(filtered_counts.items()))
        log_to_file(self.log_file, f"filtered: {items}")


def split_for_discord(
    text: str,
    *,
    max_len: int = 750,
    max_parts: int = 5,
    max_sentences_per_chunk: int = 5,
) -> list[str]:
    """Split text into Discord-safe chunks.
    
    Strategy:
    - Prefer sentence boundaries
    - Aim for <= max_sentences_per_chunk sentences per message
    - Respect max_len (Discord has a hard 2000 char limit)
    - Cap at max_parts to avoid spam
    """
    text = (text or "").strip()
    if not text:
        return []
    
    # Normalize whitespace/newlines
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    
    # Sentence-ish splitting (keeps punctuation)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    
    chunks: list[str] = []
    buf: list[str] = []
    
    def flush() -> None:
        nonlocal buf
        if buf:
            chunks.append(" ".join(buf).strip())
        buf = []
    
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        
        # If a single sentence is huge, hard-split it
        if len(s) > max_len:
            flush()
            start = 0
            while start < len(s) and len(chunks) < max_parts:
                chunks.append(s[start:start + max_len].strip())
                start += max_len
            continue
        
        candidate = " ".join(buf + [s]).strip()
        if len(candidate) <= max_len and (len(buf) + 1) <= max_sentences_per_chunk:
            buf.append(s)
        else:
            flush()
            buf.append(s)
        
        if len(chunks) >= max_parts:
            break
    
    if len(chunks) < max_parts:
        flush()
    
    # If we hit the cap, add a visible truncation marker
    if chunks and len(chunks) == max_parts:
        chunks[-1] = chunks[-1].rstrip() + " …"
    
    return chunks[:max_parts]


def chunk_text_for_tts(
    text: str,
    max_chars: int = 260,
    max_parts: int = 6,
) -> list[str]:
    """Split text into short chunks for TTS (sentence-aware)."""
    text = (text or "").strip()
    if not text:
        return []
    
    text = re.sub(r"\s+", " ", text).strip()
    sentences = re.split(r"(?<=[.!?])\s+", text)
    
    chunks: list[str] = []
    buf = ""
    
    def flush() -> None:
        nonlocal buf
        if buf.strip():
            chunks.append(buf.strip())
        buf = ""
    
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        
        if len(buf) + len(s) + 1 <= max_chars:
            buf = (buf + " " + s).strip()
        else:
            flush()
            if len(s) <= max_chars:
                buf = s
            else:
                # Hard split long sentences
                start = 0
                while start < len(s) and len(chunks) < max_parts:
                    chunks.append(s[start:start + max_chars].strip())
                    start += max_chars
                buf = ""
        
        if len(chunks) >= max_parts:
            break
    
    flush()
    
    if len(chunks) > max_parts:
        chunks = chunks[:max_parts]
    
    # Add truncation marker if text was truncated
    if chunks and len(text) > sum(len(c) for c in chunks) + (len(chunks) - 1):
        chunks[-1] = chunks[-1].rstrip() + " …"
    
    return chunks


def truncate_for_tts(text: str, max_chars: int = 600) -> str:
    """Keep auto-TTS short so it doesn't drone in VC."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "..."
