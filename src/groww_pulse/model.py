"""Model client used by clustering and compose.

Tests pass a fake. The live client calls Anthropic's Messages API.
``PULSE_MODEL`` overrides the model name. ``ANTHROPIC_API_KEY`` is the credential.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Protocol

DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_MODEL_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
MAX_TOKENS = 4096


class ModelClient(Protocol):
    """One prompt in, one text completion out."""

    def complete(self, prompt: str) -> str:
        """Return the model text for ``prompt``."""


class ModelError(RuntimeError):
    """The live model client could not complete a prompt."""


class LiveModelClient:
    """Messages API client aimed at Claude Haiku 5.5.

    The model defaults to ``claude-haiku-5-5``. ``PULSE_MODEL`` overrides
    that name. The key is ``ANTHROPIC_API_KEY``, unless ``PULSE_API_KEY``
    is set. ``PULSE_MODEL_BASE_URL`` overrides the API host.
    """

    def __init__(self, model: str, *, api_key: str, base_url: str) -> None:
        self.model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    @classmethod
    def from_env(cls) -> LiveModelClient:
        """Build a client from the environment, or raise ``ModelError``."""
        model = os.environ.get("PULSE_MODEL", "").strip() or DEFAULT_MODEL
        api_key = (
            os.environ.get("PULSE_API_KEY", "").strip()
            or os.environ.get("ANTHROPIC_API_KEY", "").strip()
        )
        if not api_key:
            raise ModelError("ANTHROPIC_API_KEY or PULSE_API_KEY is required")
        base_url = os.environ.get("PULSE_MODEL_BASE_URL", DEFAULT_MODEL_BASE_URL).strip()
        if not base_url:
            base_url = DEFAULT_MODEL_BASE_URL
        return cls(model, api_key=api_key, base_url=base_url)

    def complete(self, prompt: str) -> str:
        """Send one user message and return the assistant text."""
        body = json.dumps(
            {
                "model": self.model,
                "max_tokens": MAX_TOKENS,
                "thinking": {"type": "disabled"},
                "output_config": {"effort": "low"},
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/v1/messages",
            data=body,
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "Content-Type": "application/json",
                "User-Agent": "groww-pulse/0.0.1",
            },
            method="POST",
        )
        payload = self._post(request)
        if payload.get("stop_reason") == "refusal":
            raise ModelError("model refused the prompt")
        content = _message_text(payload)
        if not content:
            raise ModelError("model response did not include a message")
        return content

    def _post(self, request: urllib.request.Request) -> dict[str, object]:
        """POST one message, waiting out a short rate-limit window."""
        last_error: Exception | None = None
        for attempt in range(8):
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last_error = exc
                detail = exc.read().decode("utf-8", errors="replace")
                spend_capped = "enforced_spend_limit_reached" in detail
                rate_limited = exc.code in (429, 529) and not spend_capped
                if not rate_limited or attempt == 7:
                    snippet = " ".join(detail.split())[:180]
                    message = f"model request failed: {exc}"
                    if snippet:
                        message = f"{message}: {snippet}"
                    raise ModelError(message) from exc
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    wait = float(retry_after) if retry_after else 5.0
                except ValueError:
                    wait = 5.0
                time.sleep(min(max(wait, 1.0), 60.0))
                continue
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt == 7:
                    raise ModelError(f"model request failed: {exc}") from exc
                time.sleep(5.0)
                continue
            except json.JSONDecodeError as exc:
                raise ModelError("model response was not JSON") from exc
            if not isinstance(payload, dict):
                raise ModelError("model response was not an object")
            return payload
        raise ModelError(f"model request failed: {last_error}")


def _message_text(payload: dict[str, object]) -> str:
    """Join text blocks. A Haiku reply can also contain thinking blocks."""
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n".join(parts)
