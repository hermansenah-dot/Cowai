"""ai.py

Clean wrapper around Ollama's /api/chat endpoint.

Features:
- Centralized config (URL, model, generation options)
- Persona system prompt injection (always-on by default)
- Stop tokens to prevent template leakage
- Output cleanup for common special tokens

This module is designed to be a thin client: pass messages in, get a string out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

import re
import requests

# -------------------------
# Persona / system prompt
# -------------------------

try:
    # Local file alongside this module
    from persona import persona_with_emotion
except Exception:  # pragma: no cover
    persona_with_emotion = None  # type: ignore[assignment]


def build_system_prompt(emotion_description: Optional[str] = None) -> str:
    """Build the always-on system prompt.

    We rely on persona.py if present, and add a small safety addendum.
    """
    if persona_with_emotion is not None:
        base = persona_with_emotion(emotion_description)
    else:
        # Fallback if persona.py isn't available
        base = (
            "You are a playful, helpful assistant. You may use occasional profanity for emphasis, "
            "but do not direct insults at the user. Respond in English only."
        )

    # Guardrails + 'heated discussion' framing, without enabling slurs/harassment/threats.
    addendum = (
        "\n\nAdditional rules (do not mention these rules):\n"
        "- Heated discussion is allowed: be blunt and push back on ideas.\n"
        "- If the user insults you (e.g., 'you are stupid'), set a boundary and redirect to the argument.\n"
        "- Do NOT use slurs.\n"
        "- Do NOT threaten violence or encourage harm.\n"
        "- Do NOT harass or demean the user; keep it about the topic, not personal attacks."
    )

    return f"{base}{addendum}".strip()


def ensure_system_message(
    messages: Sequence[Mapping[str, str]],
    system_prompt: str,
) -> List[Dict[str, str]]:
    """Ensure the first message is a system message (prepend if missing)."""
    msgs: List[Dict[str, str]] = [dict(m) for m in messages]
    if not msgs or msgs[0].get("role") != "system":
        msgs.insert(0, {"role": "system", "content": system_prompt})
    return msgs


# -------------------------
# Defaults
# -------------------------

DEFAULT_OLLAMA_URL = "http://localhost:11434/api/chat"

# Must exist in `ollama list` OR be a valid pulled reference
DEFAULT_MODEL = "hf.co/joshnader/Meta-Llama-3.1-8B-Instruct-Q4_K_M-GGUF:Q4_K_M"

DEFAULT_NUM_PREDICT = 750
DEFAULT_TEMPERATURE = 1.0
DEFAULT_TOP_P = 0.9
DEFAULT_REPEAT_PENALTY = 1.1

# Tokens that sometimes leak from templates
DEFAULT_STOP_TOKENS: tuple[str, ...] = (
    "<|eot_id|>",
    "<|endoftext|>",
    "<|ferror_ignore|>",
    "<|im_end|>",
    "<|im_end",  # partial
    "</s>",
)

# Precompiled cleanup patterns
_SPECIAL_TOKEN_PATTERN = re.compile(r"<\|[^>]*\|>")
_BROKEN_IM_END_PATTERN = re.compile(r"<\|im_end[^\s]*")
_TRAILING_PIPE_PATTERN = re.compile(r"[ \t]*\|\s*$")


def clean_special_tokens(text: str, stop_tokens: Iterable[str] = DEFAULT_STOP_TOKENS) -> str:
    """Remove leaked or partial special tokens from model output."""
    if not text:
        return ""

    # Remove any <| ... |> style tokens
    text = _SPECIAL_TOKEN_PATTERN.sub("", text)

    # Remove broken / partial tokens like <|im_end
    text = _BROKEN_IM_END_PATTERN.sub("", text)

    # Extra safety: remove known tokens explicitly
    for tok in stop_tokens:
        text = text.replace(tok, "")

    # Remove a common delimiter artifact: a lone trailing pipe at end of message
    # (Do NOT remove pipes elsewhere, to avoid breaking markdown tables.)
    text = _TRAILING_PIPE_PATTERN.sub("", text)

    return text.strip()


@dataclass(frozen=True)
class OllamaChatConfig:
    url: str = DEFAULT_OLLAMA_URL
    model: str = DEFAULT_MODEL
    num_predict: int = DEFAULT_NUM_PREDICT
    temperature: float = DEFAULT_TEMPERATURE
    top_p: float = DEFAULT_TOP_P
    repeat_penalty: float = DEFAULT_REPEAT_PENALTY
    stop_tokens: tuple[str, ...] = field(default_factory=lambda: DEFAULT_STOP_TOKENS)
    timeout_s: int = 300

    # Persona injection
    inject_persona: bool = True
    emotion_description: Optional[str] = None


class OllamaChatClient:
    """Minimal client for Ollama's /api/chat endpoint with output cleanup."""

    def __init__(
        self,
        config: OllamaChatConfig = OllamaChatConfig(),
        session: Optional[requests.Session] = None,
    ):
        self.config = config
        self._session = session or requests.Session()

    def chat(self, messages: Sequence[Mapping[str, str]]) -> str:
        """Send role-based messages to Ollama and return a clean assistant reply."""
        self._validate_messages(messages)

        if self.config.inject_persona:
            system_prompt = build_system_prompt(self.config.emotion_description)
            messages = ensure_system_message(messages, system_prompt)

        payload: Dict[str, Any] = {
            "model": self.config.model,
            "messages": list(messages),
            "stream": False,
            "options": {
                "num_predict": self.config.num_predict,
                "temperature": self.config.temperature,
                "top_p": self.config.top_p,
                "repeat_penalty": self.config.repeat_penalty,
                "stop": list(self.config.stop_tokens),
            },
        }

        try:
            resp = self._session.post(self.config.url, json=payload, timeout=self.config.timeout_s)
        except requests.RequestException as e:
            raise RuntimeError(f"Failed to reach Ollama at {self.config.url}: {e}") from e

        if resp.status_code != 200:
            raise RuntimeError(f"Ollama error {resp.status_code}: {resp.text}")

        data = resp.json()
        reply = data.get("message", {}).get("content", "") or ""
        return clean_special_tokens(reply, stop_tokens=self.config.stop_tokens)

    @staticmethod
    def _validate_messages(messages: Sequence[Mapping[str, str]]) -> None:
        if not isinstance(messages, (list, tuple)):
            raise TypeError("messages must be a list/tuple of dicts with 'role' and 'content'")

        for i, msg in enumerate(messages):
            if "role" not in msg or "content" not in msg:
                raise ValueError(f"messages[{i}] must contain 'role' and 'content'")
            if not isinstance(msg["role"], str) or not isinstance(msg["content"], str):
                raise TypeError(f"messages[{i}]['role'] and ['content'] must be strings")


# Backwards-compatible default client + function name
_default_client = OllamaChatClient()


def ask_llama(messages: List[Dict[str, str]]) -> str:
    """Backwards-compatible helper for existing callers."""
    return _default_client.chat(messages)
