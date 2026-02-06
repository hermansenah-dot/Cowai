
"""
TL;DR module: Summarize a webpage from a URL using AI (Ollama).
Uses BeautifulSoup4 for extraction, always returns bullet points, and supports up to 2000 tokens.
"""

import re
import requests
import asyncio
from bs4 import BeautifulSoup
from ai import ask_llama
import discord
import logging

URL_REGEX = re.compile(r"https?://\S+")

async def extract_main_text(url: str) -> str:
    """Extract main visible text from a webpage using BeautifulSoup4."""
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        # Only keep content below 'New Additions and Improvements'
        marker = "New Additions and Improvements"
        idx = text.find(marker)
        if idx != -1:
            text = text[idx + len(marker):]
        lines = [line for line in text.splitlines() if line.strip()]
        main_text = "\n".join(lines)
        if not main_text:
            return "[No article content found.]"
        return main_text[:3000]
    except Exception as e:
        return f"[Error extracting page: {e}]"

async def summarize_text(text: str) -> str:
    """Summarize the given text using AI, always as bullet points."""
    messages = [
        {
            "role": "user",
            "content": (
                "Summarize the following webpage as bullet points, focusing on the main points. "
                "Use clear, concise bullets.\n\n" + text
            ),
        }
    ]
    summary = await asyncio.to_thread(ask_llama, messages, 2000)
    return summary.strip()

async def handle_tldr_command(message: discord.Message, url: str) -> None:
    """Fetch, summarize, and reply with a TL;DR for the given URL."""
    logging.info(f"[TL;DR] Triggered for URL: {url}")
    await message.channel.send("Fetching and summarizing the page, please wait...")
    text = await extract_main_text(url)
    if text.startswith("[Error"):
        await message.channel.send(text)
        return
    summary = await summarize_text(text)
    await message.channel.send(f"**TL;DR:**\n{summary}")

def extract_url_from_message(content: str) -> str | None:
    match = URL_REGEX.search(content)
    return match.group(0) if match else None
