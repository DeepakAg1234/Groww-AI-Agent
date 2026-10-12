"""MCP client for the hosted Google Workspace server.

The live client speaks Streamable HTTP. It does not store a bearer token,
a Google refresh token, or a client secret. Tests pass a fake ``call_tool``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Protocol

DEFAULT_MCP_URL = "https://mcp-server-production-af95.up.railway.app/mcp"
PROTOCOL_VERSION = "2025-03-26"


class McpClient(Protocol):
    """One tool name and its arguments in, one result envelope out."""

    def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        """Return the tool envelope (``ok``, ``result`` or ``error``)."""


class McpError(RuntimeError):
    """The MCP transport failed before a tool envelope was available."""


class LiveMcpClient:
    """Streamable HTTP client for google-workspace-mcp.

    ``PULSE_MCP_URL`` overrides the hosted endpoint. ``PULSE_MCP_TOKEN`` is
    sent as a bearer when set. Neither value is written to disk.
    """

    def __init__(self, url: str, *, token: str = "") -> None:
        self.url = url
        self._token = token
        self._session_id = ""
        self._protocol = PROTOCOL_VERSION
        self._initialized = False
        self._next_id = 1

    @classmethod
    def from_env(cls) -> LiveMcpClient:
        """Build a client pointed at the hosted server."""
        url = os.environ.get("PULSE_MCP_URL", "").strip() or DEFAULT_MCP_URL
        token = os.environ.get("PULSE_MCP_TOKEN", "").strip()
        return cls(url, token=token)

    def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        """Initialize if needed, call one tool, and return its envelope."""
        self._ensure_session()
        payload = self._request(
            "tools/call",
            {"name": name, "arguments": arguments},
        )
        if "error" in payload:
            error = payload["error"]
            message = error.get("message", "MCP request failed") if isinstance(error, dict) else str(error)
            raise McpError(message)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise McpError("tool result was not an object")
        return envelope_from_tool_result(result)

    def _ensure_session(self) -> None:
        if self._initialized:
            return
        payload = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "groww-pulse", "version": "0.0.1"},
            },
            protocol_header=False,
        )
        if "error" in payload:
            error = payload["error"]
            message = error.get("message", "MCP initialize failed") if isinstance(error, dict) else str(error)
            raise McpError(message)
        result = payload.get("result")
        if isinstance(result, dict):
            negotiated = result.get("protocolVersion")
            if isinstance(negotiated, str) and negotiated.strip():
                self._protocol = negotiated.strip()
        self._request("notifications/initialized", None, protocol_header=True)
        self._initialized = True

    def _request(
        self,
        method: str,
        params: dict[str, object] | None,
        *,
        protocol_header: bool = True,
    ) -> dict[str, object]:
        message: dict[str, object] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["id"] = self._next_id
            self._next_id += 1
            message["params"] = params
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if protocol_header:
            headers["MCP-Protocol-Version"] = self._protocol
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(
            self.url,
            data=json.dumps(message).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                session = response.headers.get("Mcp-Session-Id", "")
                if session:
                    self._session_id = session
                body = response.read().decode("utf-8")
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise McpError(f"MCP HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise McpError(f"MCP request failed: {exc.reason}") from exc
        if not body.strip():
            return {}
        if "text/event-stream" in content_type:
            return _parse_sse(body)
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise McpError("MCP response was not JSON") from exc
        if not isinstance(parsed, dict):
            raise McpError("MCP response was not an object")
        return parsed


def envelope_from_tool_result(result: dict[str, object]) -> dict[str, object]:
    """Read ``structuredContent``, or the same JSON in ``content[0].text``."""
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and "ok" in structured:
        envelope: dict[str, object] = dict(structured)
    else:
        envelope = _envelope_from_content(result.get("content"))
    if result.get("isError") is True and envelope.get("ok") is not False:
        error = envelope.get("error")
        if not isinstance(error, dict):
            error = {
                "code": "TOOL_ERROR",
                "message": "tool returned isError",
                "retryable": False,
            }
        return {
            "ok": False,
            "operation_id": str(envelope.get("operation_id", "")),
            "tool": str(envelope.get("tool", "")),
            "error": error,
        }
    return envelope


def _envelope_from_content(content: object) -> dict[str, object]:
    if not isinstance(content, list) or not content:
        raise McpError("tool result has no envelope")
    first = content[0]
    if not isinstance(first, dict):
        raise McpError("tool result has no envelope")
    text = first.get("text")
    if not isinstance(text, str) or not text.strip():
        raise McpError("tool result has no envelope")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpError("tool result text was not JSON") from exc
    if not isinstance(parsed, dict) or "ok" not in parsed:
        raise McpError("tool result text was not an envelope")
    return parsed


def _parse_sse(body: str) -> dict[str, object]:
    """Return the last JSON object carried in an SSE ``data`` field."""
    found: dict[str, object] | None = None
    data_lines: list[str] = []

    def flush() -> None:
        nonlocal found
        if not data_lines:
            return
        raw = "\n".join(data_lines).strip()
        data_lines.clear()
        if not raw or raw == "[DONE]":
            return
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            found = parsed

    for line in body.splitlines():
        if line == "":
            flush()
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    flush()
    if found is None:
        raise McpError("MCP event stream had no JSON payload")
    return found
