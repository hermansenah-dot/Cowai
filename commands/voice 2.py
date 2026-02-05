
"""Voice/TTS commands for Cowai bot (Maicé)."""

import time
import asyncio
import discord
from pathlib import Path
from utils.text import chunk_text_for_tts

VOICE_ENABLED = True

def get_voice_enabled(user_id: int, LongMemory) -> bool:
    return VOICE_ENABLED

def set_voice_enabled(user_id: int, enabled: bool, LongMemory) -> None:
    global VOICE_ENABLED
    VOICE_ENABLED = bool(enabled)

async def handle_tts(message: discord.Message, content: str) -> bool:
    text = content[4:].strip()
    if not text:
        await message.channel.send("Usage: `!tts <text>`")
        return True
    guild = message.guild
    if not guild:
        await message.channel.send("This command only works in a server.")
        return True
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        await message.channel.send("I'm not in a voice channel. Use `!join` first.")
        return True
    from voice.tts import handle_tts_command
    await handle_tts_command(text)
    return True

async def maybe_auto_voice_reply(message: discord.Message, reply: str, LongMemory) -> None:
    """
    Speak AI reply in VC if the author has !voice on and is in a voice channel.
    Safe to call after sending the text reply.
    """
    if not get_voice_enabled(message.author.id, LongMemory):
        return
    if not (hasattr(message.author, 'voice') and message.author.voice and hasattr(message.author.voice, 'channel') and message.author.voice.channel):
        return
    lines = chunk_text_for_tts(reply, max_chars=260, max_parts=6)
        try:
            from voice.tts import handle_tts_lines, _find_ffmpeg_exe
            paths = await handle_tts_lines(lines, backend="edge")
            ffmpeg_exe = _find_ffmpeg_exe()
            guild = message.guild
            vc = guild.voice_client if guild else None
            if vc and vc.is_connected():
                for path in paths:
                    try:
                        audio = discord.FFmpegPCMAudio(str(path), executable=ffmpeg_exe)
                        done = asyncio.Event()
                        def after_play(err):
                            done.set()
                        vc.play(audio, after=after_play)
                        await done.wait()
                    except Exception as e:
                        pass
        except Exception:
            pass


async def handle_join(message: discord.Message, content: str) -> bool:
    """
    Joins the user's current voice channel.
    """
    if not message.guild:
        await message.channel.send("This command only works in a server.")
        return True
    if not (message.author.voice and message.author.voice.channel):
        await message.channel.send("You must be in a voice channel to use `!join`.")
        return True
    channel = message.author.voice.channel
    set_voice_enabled(None, True, None)
    try:
        vc = message.guild.voice_client
        if vc and vc.is_connected():
            await vc.move_to(channel)
            await message.channel.send(f"Moved to {channel.mention}.")
        else:
            # Use VoiceRecvClient if available
            voice_client_class = None
            try:
                from discord.ext.voice_recv import VoiceRecvClient
                voice_client_class = VoiceRecvClient
            except ImportError:
                pass
            if voice_client_class:
                vc = await channel.connect(cls=voice_client_class)
            else:
                vc = await channel.connect()
            await message.channel.send(f"Joined {channel.mention}.")
        # Start STT listening if possible
            try:
                from core.stt import start_listening
                from core.conversation import handle_ai_conversation
                async def on_transcription(member, text):
                    # Create a fake Discord message for the member
                    class FakeVoice:
                        def __init__(self, channel):
                            self.channel = channel
                    class FakeAuthor:
                        def __init__(self, member):
                            self.id = member.id
                            self.display_name = member.display_name
                            self.voice = FakeVoice(message.author.voice.channel if message.author.voice else channel)
                    class FakeMessage:
                        def __init__(self, author, channel, content):
                            self.author = author
                            self.channel = channel
                            self.content = content
                            self.guild = channel.guild
                            self.id = int(time.time() * 1000)  # Unique fake ID
                    fake_author = FakeAuthor(member)
                    fake_message = FakeMessage(fake_author, message.channel, text)
                    await message.channel.send(f"**{member.display_name} said:** {text}")
                    await handle_ai_conversation(fake_message, text)
                await start_listening(vc, message.channel, on_transcription)
            except Exception as stt_e:
                await message.channel.send(f"[STT] Could not start listening: {stt_e}")
    except Exception as e:
        await message.channel.send(f"Failed to join voice channel: {e}")
    return True
