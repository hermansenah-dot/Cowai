"""
Automated TTS test script for Cowai bot.
Generates TTS outputs for sample messages and logs results for review.
"""

import asyncio
from pathlib import Path
from voice.tts import handle_tts_command
from utils.context_analyzer import analyze_message_context

SAMPLES = [
    "Hello, how are you?",
    "I am so happy today!",
    "Why did you do that?",
    "This is annoying...",
    "maicé, can you help me?",
    "What is the meaning of life?",
    "I love this bot!",
    "I'm sad about the news.",
]

async def test_tts_samples():
    for msg in SAMPLES:
        context = analyze_message_context(msg, bot_name="maicé")
        emotion = "emotion" if "emotion" in context.get("tags", []) else None
        print(f"Testing: '{msg}' | Emotion: {emotion}")
        output_path = await handle_tts_command(msg, backend="edge", emotion=emotion)
        print(f"Output file: {output_path}")

if __name__ == "__main__":
    asyncio.run(test_tts_samples())
