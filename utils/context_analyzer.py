"""
Context analyzer for TTS triggering in Cowai bot.
Analyzes message intent, relevance, and emotion to decide if TTS should respond.
"""

import re
from typing import Dict, Any

# Example keywords for intent/emotion detection
QUESTION_WORDS = ["who", "what", "when", "where", "why", "how", "?"]
GREETINGS = ["hello", "hi", "hey", "greetings"]
EMOTION_WORDS = ["love", "hate", "angry", "happy", "sad", "excited", "annoyed"]


def analyze_message_context(message: str, user_id: int = None, bot_name: str = "maicé") -> Dict[str, Any]:
    """
    Analyze message for intent, relevance, and emotion.
    Returns a dict with tags and a score.
    """
    tags = set()
    score = 0
    msg = message.lower()

    # Intent: Question
    if any(q in msg for q in QUESTION_WORDS):
        tags.add("question")
        score += 2

    # Intent: Direct mention
    if bot_name in msg:
        tags.add("mention")
        score += 2

    # Greeting
    if any(greet in msg for greet in GREETINGS):
        tags.add("greeting")
        score += 1

    # Emotion
    if any(em in msg for em in EMOTION_WORDS):
        tags.add("emotion")
        score += 1

    # Length/relevance
    if len(msg) > 20:
        score += 1

    # Example: Only trigger TTS if score >= 2
    should_tts = score >= 2

    return {
        "tags": list(tags),
        "score": score,
        "should_tts": should_tts
    }
