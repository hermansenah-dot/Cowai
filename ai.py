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
import json
import requests

from utils.helpers import clamp

# -------------------------
# Persona / system prompt
# -------------------------

try:
    # Local file alongside this module
    from personality.persona import persona_with_emotion
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
DEFAULT_MODEL = "llama3.1:8b"

DEFAULT_NUM_PREDICT = 400
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
    # Common role markers that cause the model to continue as the other side
    "\nUser:",
    "\nuser:",
    "\nAssistant:",
    "\nassistant:",
    "\nSystem:",
    "\nsystem:",
    "\n### User",
    "\n### Assistant",
    # Llama-style header tokens (may appear depending on template)
    "<|start_header_id|>user",
    "<|start_header_id|>assistant",
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


# -------------------------
# Telemetry / transcript sanitization
# -------------------------
# Some apps accidentally feed internal UI telemetry (timestamps, trust scores, emotion stats)
# back into the model, and the model then echoes it. These helpers strip such artifacts
# from both inputs and outputs defensively.

_TELEMETRY_DROP_RE = re.compile(
    r"""(?ix)
    ^\[\d{1,2}:\d{2}\]            # [18:12]
    |^app\s*$                       # APP
    |^maic[eé]:                      # mAIcé:
    |\btrust\s*score\b
    |\bvalence\b
    |\barousal\b
    |\bdominance\b
    |\bresponse\s*time\b
    |\bstyle\s*tics\b
    |\bconversation\s*style\s*rules\b
    """
)

_TELEMETRY_TRUNC_RE = re.compile(
    r"""(?ix)
    (\n\s*app\s*\n)
    |(\n\s*maic[eé]:)
    |(\n\s*\[\d{1,2}:\d{2}\])
    """
)


def _sanitize_from_llm(text: str) -> str:
    """Strip obvious telemetry/transcript artifacts from model output."""
    if not text:
        return ""
    # Truncate at first strong marker
    m = _TELEMETRY_TRUNC_RE.search(text)
    if m:
        text = text[: m.start()].strip()

    lines = text.splitlines()
    kept: List[str] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            kept.append(ln)
            continue
        if _TELEMETRY_DROP_RE.search(s):
            continue
        kept.append(ln)

    text = "\n".join(kept).strip()

    # Strip repeated inline "|Note: ...|" artifacts (common when prompt blocks include meta notes).
    # We only remove the Note segment up to the next pipe/newline/end, leaving the rest intact.
    text = re.sub(r"\s*\|\s*note:.*?(?=\s*\||\n|$)", "", text, flags=re.IGNORECASE)

    # Collapse accidental extra spaces introduced by removals.
    text = re.sub(r"[ \t]{2,}", " ", text)

    return text.strip()


def _sanitize_messages_for_llm(messages: Sequence[Mapping[str, str]]) -> List[Dict[str, str]]:
    """Remove telemetry lines from message contents before sending to the model."""
    out: List[Dict[str, str]] = []
    for m in messages:
        role = str(m.get("role", ""))
        content = str(m.get("content", ""))
        if content:
            content = _sanitize_from_llm(content)
        out.append({"role": role, "content": content})
    return out


@dataclass(frozen=True)
class OllamaChatConfig:
    url: str = DEFAULT_OLLAMA_URL
    model: str = DEFAULT_MODEL
    num_predict: int = DEFAULT_NUM_PREDICT
    temperature: float = DEFAULT_TEMPERATURE
    top_p: float = DEFAULT_TOP_P
    repeat_penalty: float = DEFAULT_REPEAT_PENALTY
    stop_tokens: tuple[str, ...] = field(default_factory=lambda: DEFAULT_STOP_TOKENS)
    timeout_s: int = 120

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

        if self.config.inject_persona:
            system_prompt = build_system_prompt(self.config.emotion_description)
            messages = ensure_system_message(messages, system_prompt)

        # Defensive cleanup: strip any UI telemetry that may have slipped into history.
        messages = _sanitize_messages_for_llm(messages)
        self._validate_messages(messages)

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

        # Retry logic with exponential backoff for timeouts
        max_retries = 3
        base_delay = 2.0
        last_error: Optional[Exception] = None

        for attempt in range(max_retries):
            try:
                resp = self._session.post(self.config.url, json=payload, timeout=self.config.timeout_s)
                if resp.status_code != 200:
                    raise RuntimeError(f"Ollama error {resp.status_code}: {resp.text}")
                break  # Success, exit retry loop
            except requests.exceptions.Timeout as e:
                last_error = e
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)  # 2s, 4s, 8s
                    print(f"[ai] Timeout on attempt {attempt + 1}/{max_retries}, retrying in {delay}s...")
                    import time
                    time.sleep(delay)
                else:
                    raise RuntimeError(f"Ollama timed out after {max_retries} attempts: {e}") from e
            except requests.RequestException as e:
                raise RuntimeError(f"Failed to reach Ollama at {self.config.url}: {e}") from e

        data = resp.json()
        reply = data.get("message", {}).get("content", "") or ""
        reply = clean_special_tokens(reply, stop_tokens=self.config.stop_tokens)
        reply = _sanitize_from_llm(reply)
        return reply

    @staticmethod
    def _validate_messages(messages: Sequence[Mapping[str, str]]) -> None:
        if not isinstance(messages, (list, tuple)):
            raise TypeError("messages must be a list/tuple of dicts with 'role' and 'content'")

        for i, msg in enumerate(messages):
            if "role" not in msg or "content" not in msg:
                raise ValueError(f"messages[{i}] must contain 'role' and 'content'")
            if not isinstance(msg["role"], str) or not isinstance(msg["content"], str):
                raise TypeError(f"messages[{i}]['role'] and ['content'] must be strings")



# -------------------------
# NLP analysis (starter module)
# -------------------------
#
# This is intentionally small + strict so you can build on it later.
# It returns structured signals (intent/emotion/needs/topic) as a dict,
# and NEVER includes the JSON in chat history by default.

_NLP_SYSTEM_PROMPT = (
    "You are an NLP classifier for a chat assistant. "
    "Return EXACTLY one JSON object and nothing else. "
    "No markdown, no explanations.\n\n"
    "Schema:\n"
    "{\n"
    "  \"intent\": \"question|request|venting|debate|insult|smalltalk|other\",\n"
    "  \"is_question\": true|false,\n"
    "  \"topic\": \"short noun phrase or empty\",\n"
    "  \"emotion\": { \"label\": \"angry|frustrated|sad|anxious|happy|excited|neutral|other\", "
    "\"valence\": -1.0..1.0, \"arousal\": 0.0..1.0 },\n"
    "  \"needs\": [\"validation\", \"solution\", \"reassurance\", \"boundary\", \"clarification\"]\n"
    "}\n\n"
    "Rules:\n"
    "- If the message is an insult toward the assistant/user, intent=insult and include needs=['boundary'].\n"
    "- If the user is venting without a clear question, intent=venting and include needs like validation/reassurance.\n"
    "- Keep topic short (1-5 words). Use empty string if unclear.\n"
    "- Clamp numeric ranges.\n"
)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_first_json_object(text: str) -> Optional[str]:
    if not text:
        return None
    text = text.strip()
    # Fast path: whole string is JSON
    if text.startswith("{") and text.endswith("}"):
        return text
    m = _JSON_OBJECT_RE.search(text)
    if m:
        return m.group(0)
    return None


def _normalize_nlp_result(obj: Any) -> Dict[str, Any]:
    # Safe defaults
    out: Dict[str, Any] = {
        "intent": "other",
        "is_question": False,
        "topic": "",
        "emotion": {"label": "neutral", "valence": 0.0, "arousal": 0.3},
        "needs": [],
    }
    if not isinstance(obj, dict):
        return out

    intent = str(obj.get("intent", out["intent"])).strip().lower()
    if intent not in {"question", "request", "venting", "debate", "insult", "smalltalk", "other"}:
        intent = "other"
    out["intent"] = intent

    out["is_question"] = bool(obj.get("is_question", False))

    topic = str(obj.get("topic", "")).strip()
    # Avoid huge topics
    if len(topic) > 80:
        topic = topic[:80].rstrip()
    out["topic"] = topic

    emo = obj.get("emotion", {})
    if isinstance(emo, dict):
        label = str(emo.get("label", "neutral")).strip().lower()
        if label not in {"angry", "frustrated", "sad", "anxious", "happy", "excited", "neutral", "other"}:
            label = "other"
        out["emotion"] = {
            "label": label,
            "valence": clamp(emo.get("valence", 0.0), -1.0, 1.0),
            "arousal": clamp(emo.get("arousal", 0.3), 0.0, 1.0),
        }

    needs = obj.get("needs", [])
    if isinstance(needs, list):
        cleaned = []
        allowed = {"validation", "solution", "reassurance", "boundary", "clarification"}
        for n in needs:
            s = str(n).strip().lower()
            if s in allowed and s not in cleaned:
                cleaned.append(s)
        out["needs"] = cleaned

    # Derive is_question if missing but ends with '?'
    if "is_question" not in obj and isinstance(obj.get("raw", None), str):
        out["is_question"] = obj["raw"].rstrip().endswith("?")

    # Strong heuristic: if intent=insult, ensure boundary need
    if out["intent"] == "insult" and "boundary" not in out["needs"]:
        out["needs"] = ["boundary"] + out["needs"]

    return out


def analyze_nlp(
    user_text: str,
    context: Optional[Sequence[Mapping[str, str]]] = None,
    *,
    url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    timeout_s: int = 60,
) -> Dict[str, Any]:
    """Return a small NLP analysis dict for the user's latest message.

    This is meant to be called OUTSIDE the main chat history and used as a hint.
    """
    user_text = (user_text or "").strip()
    if not user_text:
        return _normalize_nlp_result({})

    ctx_lines: List[str] = []
    if context:
        # Keep context short and clean
        for m in list(context)[-6:]:
            role = str(m.get("role", "")).strip().lower()
            content = str(m.get("content", "")).strip()
            if role not in {"user", "assistant"}:
                continue
            if not content:
                continue
            content = content.replace("\n", " ").strip()
            if len(content) > 200:
                content = content[:200].rstrip() + "…"
            ctx_lines.append(f"{role}: {content}")

    user_block = "User message:\n" + user_text
    if ctx_lines:
        user_block += "\n\nRecent context (most recent last):\n" + "\n".join(ctx_lines)

    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": _NLP_SYSTEM_PROMPT},
            {"role": "user", "content": user_block},
        ],
        "stream": False,
        "options": {
            "num_predict": 220,
            "temperature": 0.0,
            "top_p": 0.2,
            "repeat_penalty": 1.0,
            "stop": ["\n\n", "<|eot_id|>", "</s>"],
        },
    }

    # Use a short-lived session for analysis to avoid interfering with chat config.
    try:
        resp = requests.post(url, json=payload, timeout=timeout_s)
    except Exception:
        return _normalize_nlp_result({})

    if resp.status_code != 200:
        return _normalize_nlp_result({})

    try:
        data = resp.json()
        raw = data.get("message", {}).get("content", "") or ""
    except Exception:
        raw = ""

    raw = clean_special_tokens(raw, stop_tokens=DEFAULT_STOP_TOKENS)
    raw = _sanitize_from_llm(raw)

    js = _extract_first_json_object(raw)
    if not js:
        return _normalize_nlp_result({})

    try:
        obj = json.loads(js)
    except Exception:
        return _normalize_nlp_result({})

    return _normalize_nlp_result(obj)


# Backwards-compatible default client + function name
_default_client = OllamaChatClient()


def ask_llama(messages: List[Dict[str, str]]) -> str:
    """Backwards-compatible helper for existing callers."""
    return _default_client.chat(messages)
