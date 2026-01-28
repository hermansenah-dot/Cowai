"""
ai.py

Clean wrapper around Ollama's /api/chat endpoint.

Responsibilities:
- Centralize model + endpoint configuration
- Apply consistent generation settings
- Stop generation on special tokens
- Strip leaked or partial special tokens from output
"""

from __future__ import annotations

from typing import Any, Dict, List
import requests
import re

# =========================

OLLAMA_URL = "http://localhost:11434/api/chat"

# Must exist in `ollama list` OR be a valid pulled reference

# Tokens that sometimes leak from templates
STOP_TOKENS = [
    "<|eot_id|>",
    "<|endoftext|>",
    "<|ferror_ignore|>",
    "<|im_end|>",
    "<|im_end",      # partial
    "</s>",
]

print("ai.py loaded (token cleanup enabled)")


# =========================
# Output cleanup
# =========================

def clean_special_tokens(text: str) -> str:
    """
    Remove leaked or partial special tokens from model output.
    Handles both full tokens (<|im_end|>) and broken ones (<|im_end).
    """

    # Remove any <| ... |> style tokens
    text = re.sub(r"<\|[^>]*\|>", "", text)

    # Remove broken / partial tokens like <|im_end
    text = re.sub(r"<\|im_end[^\s]*", "", text)
    for tok in STOP_TOKENS:
        text = text.replace(tok, "")

    return text.strip()


def ask_llama(messages: List[Dict[str, str]]) -> str:
    """
    Send role-based messages to Ollama and return a clean assistant reply.

    messages format:
    [
        {"role": "system", "content": "..."},
        {"role": "user", "content": "..."}
    ]
    """

    payload: Dict[str, Any] = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "options": {
            "num_predict": NUM_PREDICT,
            "temperature": TEMPERATURE,
            "stop": STOP_TOKENS,
        },
    }

    r = requests.post(OLLAMA_URL, json=payload, timeout=300)

    if r.status_code != 200:
        raise RuntimeError(f"Ollama error {r.status_code}: {r.text}")

    data = r.json()
    reply = data.get("message", {}).get("content", "")
    return clean_special_tokens(reply)
