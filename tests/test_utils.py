"""Unit tests for utils package."""

import pytest
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
import tempfile
import os


# =========================
# Tests for utils/logging.py
# =========================

class TestLogging:
    """Tests for logging utilities."""
    
    def test_log_outputs_with_timestamp(self, capsys):
        """log() should print message with timestamp prefix."""
        from utils.logging import log
        
        log("test message")
        captured = capsys.readouterr()
        
        assert "test message" in captured.out
        assert "[" in captured.out  # Has timestamp brackets
        assert "]" in captured.out
    
    def test_log_to_file_creates_file(self):
        """log_to_file() should create file and write message."""
        from utils.logging import log_to_file
        
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "subdir" / "test.log"
            
            log_to_file(filepath, "test log entry")
            
            assert filepath.exists()
            content = filepath.read_text()
            assert "test log entry" in content
            assert "[" in content  # Has timestamp
    
    def test_log_to_file_appends(self):
        """log_to_file() should append to existing file."""
        from utils.logging import log_to_file
        
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "test.log"
            
            log_to_file(filepath, "first entry")
            log_to_file(filepath, "second entry")
            
            content = filepath.read_text()
            assert "first entry" in content
            assert "second entry" in content


# =========================
# Tests for utils/text.py
# =========================

class TestWordFilter:
    """Tests for WordFilter class."""
    
    def test_filter_replaces_banned_words(self):
        """WordFilter should replace banned words with replacement string."""
        from utils.text import WordFilter
        
        wf = WordFilter({"bad", "evil"}, replacement="[CENSORED]")
        
        result = wf.filter("This is a bad and evil message")
        
        assert "bad" not in result.lower()
        assert "evil" not in result.lower()
        assert "[CENSORED]" in result
    
    def test_filter_case_insensitive(self):
        """WordFilter should match case-insensitively."""
        from utils.text import WordFilter
        
        wf = WordFilter({"bad"})
        
        assert "*FILTERED!*" in wf.filter("BAD")
        assert "*FILTERED!*" in wf.filter("Bad")
        assert "*FILTERED!*" in wf.filter("bad")
    
    def test_filter_whole_word_only(self):
        """WordFilter should only match whole words."""
        from utils.text import WordFilter
        
        wf = WordFilter({"bad"})
        
        # Should filter "bad" but not "badger"
        result = wf.filter("badger is not bad")
        
        assert "badger" in result
        assert "*FILTERED!*" in result
    
    def test_filter_empty_input(self):
        """WordFilter should handle empty input gracefully."""
        from utils.text import WordFilter
        
        wf = WordFilter({"bad"})
        
        assert wf.filter("") == ""
        assert wf.filter(None) == ""
    
    def test_filter_no_banned_words(self):
        """WordFilter with empty banned set should return input unchanged."""
        from utils.text import WordFilter
        
        wf = WordFilter(set())
        
        result = wf.filter("any message here")
        assert result == "any message here"
    
    def test_filter_logs_to_file(self):
        """WordFilter should log filtered words when log_file is set."""
        from utils.text import WordFilter
        
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "censor.log"
            wf = WordFilter({"bad"}, log_file=log_file)
            
            wf.filter("this is bad")
            
            assert log_file.exists()
            content = log_file.read_text()
            assert "bad" in content


class TestLoadWordList:
    """Tests for load_word_list function."""
    
    def test_loads_words_from_file(self):
        """load_word_list should load words from file."""
        from utils.text import load_word_list
        
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "words.txt"
            filepath.write_text("word1\nword2\nword3\n")
            
            words = load_word_list(filepath)
            
            assert words == {"word1", "word2", "word3"}
    
    def test_ignores_comments(self):
        """load_word_list should ignore lines starting with #."""
        from utils.text import load_word_list
        
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "words.txt"
            filepath.write_text("# This is a comment\nword1\n# Another comment\nword2\n")
            
            words = load_word_list(filepath)
            
            assert words == {"word1", "word2"}
    
    def test_ignores_empty_lines(self):
        """load_word_list should ignore empty lines."""
        from utils.text import load_word_list
        
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "words.txt"
            filepath.write_text("word1\n\n\nword2\n   \nword3\n")
            
            words = load_word_list(filepath)
            
            assert words == {"word1", "word2", "word3"}
    
    def test_lowercases_words(self):
        """load_word_list should lowercase all words."""
        from utils.text import load_word_list
        
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "words.txt"
            filepath.write_text("WORD1\nWord2\nwOrD3\n")
            
            words = load_word_list(filepath)
            
            assert words == {"word1", "word2", "word3"}
    
    def test_missing_file_returns_empty_set(self):
        """load_word_list should return empty set for missing file."""
        from utils.text import load_word_list
        
        words = load_word_list("/nonexistent/path/words.txt")
        
        assert words == set()


class TestSplitForDiscord:
    """Tests for split_for_discord function."""
    
    def test_short_text_returns_single_chunk(self):
        """Short text should return a single chunk."""
        from utils.text import split_for_discord
        
        result = split_for_discord("Hello, world!")
        
        assert result == ["Hello, world!"]
    
    def test_respects_max_len(self):
        """Chunks should not exceed max_len (except for truncation marker)."""
        from utils.text import split_for_discord
        
        long_text = "A" * 2000
        result = split_for_discord(long_text, max_len=500, max_parts=10)
        
        # All chunks except possibly the last should be <= max_len
        for chunk in result[:-1]:
            assert len(chunk) <= 500
    
    def test_respects_max_parts(self):
        """Should not return more than max_parts chunks."""
        from utils.text import split_for_discord
        
        long_text = "A" * 5000
        result = split_for_discord(long_text, max_len=100, max_parts=3)
        
        assert len(result) <= 3
    
    def test_adds_truncation_marker(self):
        """Should add truncation marker when hitting max_parts."""
        from utils.text import split_for_discord
        
        long_text = "A" * 5000
        result = split_for_discord(long_text, max_len=100, max_parts=3)
        
        assert result[-1].endswith("…")
    
    def test_prefers_sentence_boundaries(self):
        """Should prefer splitting at sentence boundaries."""
        from utils.text import split_for_discord
        
        text = "First sentence. Second sentence. Third sentence."
        result = split_for_discord(text, max_len=100, max_parts=5)
        
        # Each chunk should end with proper punctuation (or be a continuation)
        for chunk in result:
            # Should contain complete sentences where possible
            assert "." in chunk or chunk == result[-1]
    
    def test_empty_text_returns_empty_list(self):
        """Empty text should return empty list."""
        from utils.text import split_for_discord
        
        assert split_for_discord("") == []
        assert split_for_discord("   ") == []
        assert split_for_discord(None) == []


class TestChunkTextForTts:
    """Tests for chunk_text_for_tts function."""
    
    def test_short_text_returns_single_chunk(self):
        """Short text should return a single chunk."""
        from utils.text import chunk_text_for_tts
        
        result = chunk_text_for_tts("Hello!")
        
        assert result == ["Hello!"]
    
    def test_respects_max_chars(self):
        """Chunks should respect max_chars limit."""
        from utils.text import chunk_text_for_tts
        
        text = "A" * 1000
        result = chunk_text_for_tts(text, max_chars=100, max_parts=20)
        
        for chunk in result:
            assert len(chunk) <= 100
    
    def test_respects_max_parts(self):
        """Should not return more than max_parts chunks."""
        from utils.text import chunk_text_for_tts
        
        text = "A" * 2000
        result = chunk_text_for_tts(text, max_chars=100, max_parts=3)
        
        assert len(result) <= 3
    
    def test_empty_text_returns_empty_list(self):
        """Empty text should return empty list."""
        from utils.text import chunk_text_for_tts
        
        assert chunk_text_for_tts("") == []
        assert chunk_text_for_tts("   ") == []


class TestTruncateForTts:
    """Tests for truncate_for_tts function."""
    
    def test_short_text_unchanged(self):
        """Short text should be returned unchanged."""
        from utils.text import truncate_for_tts
        
        result = truncate_for_tts("Hello, world!")
        
        assert result == "Hello, world!"
    
    def test_long_text_truncated(self):
        """Long text should be truncated with ellipsis."""
        from utils.text import truncate_for_tts
        
        text = "word " * 200  # Much longer than 600 chars
        result = truncate_for_tts(text, max_chars=50)
        
        assert len(result) <= 53  # 50 + "..."
        assert result.endswith("...")
    
    def test_truncates_at_word_boundary(self):
        """Should truncate at word boundary when possible."""
        from utils.text import truncate_for_tts
        
        text = "hello world goodbye"
        result = truncate_for_tts(text, max_chars=12)
        
        # Should cut at word boundary, not mid-word
        assert result in ["hello world...", "hello..."]


# =========================
# Tests for utils/burst.py
# =========================

class TestBurstBuffer:
    """Tests for BurstBuffer class."""
    
    @pytest.mark.asyncio
    async def test_single_message_triggers_handler(self):
        """Single message should trigger handler after timeout."""
        from utils.burst import BurstBuffer
        
        handler_called = asyncio.Event()
        received_text = []
        
        async def mock_handler(msg, text, raw_content):
            received_text.append(text)
            handler_called.set()
        
        buffer = BurstBuffer(window_s=0.1)  # Short window for testing
        buffer.set_handler(mock_handler)
        
        # Create mock message
        mock_msg = MagicMock()
        mock_msg.channel.id = 123
        mock_msg.author.id = 456
        
        await buffer.enqueue(mock_msg, "hello")
        
        # Wait for handler to be called
        await asyncio.wait_for(handler_called.wait(), timeout=1.0)
        
        assert received_text == ["hello"]
    
    @pytest.mark.asyncio
    async def test_multiple_messages_combined(self):
        """Multiple rapid messages should be combined."""
        from utils.burst import BurstBuffer
        
        handler_called = asyncio.Event()
        received_text = []
        
        async def mock_handler(msg, text, raw_content):
            received_text.append(text)
            handler_called.set()
        
        buffer = BurstBuffer(window_s=0.2)
        buffer.set_handler(mock_handler)
        
        mock_msg = MagicMock()
        mock_msg.channel.id = 123
        mock_msg.author.id = 456
        
        # Send multiple messages rapidly
        await buffer.enqueue(mock_msg, "first")
        await asyncio.sleep(0.05)
        await buffer.enqueue(mock_msg, "second")
        await asyncio.sleep(0.05)
        await buffer.enqueue(mock_msg, "third")
        
        # Wait for handler
        await asyncio.wait_for(handler_called.wait(), timeout=1.0)
        
        # Should receive combined text
        assert len(received_text) == 1
        assert "first" in received_text[0]
        assert "second" in received_text[0]
        assert "third" in received_text[0]
    
    @pytest.mark.asyncio
    async def test_different_users_separate_buffers(self):
        """Different users should have separate buffers."""
        from utils.burst import BurstBuffer
        
        handler_calls = []
        handler_done = asyncio.Event()
        call_count = 0
        
        async def mock_handler(msg, text, raw_content):
            nonlocal call_count
            handler_calls.append((msg.author.id, text))
            call_count += 1
            if call_count >= 2:
                handler_done.set()
        
        buffer = BurstBuffer(window_s=0.1)
        buffer.set_handler(mock_handler)
        
        mock_msg1 = MagicMock()
        mock_msg1.channel.id = 123
        mock_msg1.author.id = 1
        
        mock_msg2 = MagicMock()
        mock_msg2.channel.id = 123
        mock_msg2.author.id = 2
        
        await buffer.enqueue(mock_msg1, "user1 message")
        await buffer.enqueue(mock_msg2, "user2 message")
        
        await asyncio.wait_for(handler_done.wait(), timeout=1.0)
        
        # Should have separate calls for each user
        assert len(handler_calls) == 2
        user_ids = {call[0] for call in handler_calls}
        assert user_ids == {1, 2}
    
    @pytest.mark.asyncio
    async def test_max_lines_triggers_early(self):
        """Hitting max_lines should trigger handler early."""
        from utils.burst import BurstBuffer
        
        handler_called = asyncio.Event()
        
        async def mock_handler(msg, text, raw_content):
            handler_called.set()
        
        buffer = BurstBuffer(window_s=10.0, max_lines=3)  # Long window, low max_lines
        buffer.set_handler(mock_handler)
        
        mock_msg = MagicMock()
        mock_msg.channel.id = 123
        mock_msg.author.id = 456
        
        # Send enough messages to hit max_lines
        for i in range(4):
            await buffer.enqueue(mock_msg, f"line{i}")
            await asyncio.sleep(0.01)
        
        # Should trigger early despite long window
        await asyncio.wait_for(handler_called.wait(), timeout=1.0)


# =========================
# Run tests
# =========================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
