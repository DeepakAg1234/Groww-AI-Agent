"""Append a pulse note through ``gdocs_append_content``."""

from __future__ import annotations

from groww_pulse.publish.mcp import McpClient

APPEND_TOOL = "gdocs_append_content"


def append_pulse(
    client: McpClient,
    *,
    account_id: str,
    document_id: str,
    content: str,
    idempotency_key: str,
) -> dict[str, object]:
    """Append ``content`` to an existing document. Does not create or replace it."""
    return client.call_tool(
        APPEND_TOOL,
        {
            "document_id": document_id,
            "content": content,
            "account_id": account_id,
            "format": "plain_text",
            "prepend_newline": True,
            "idempotency_key": idempotency_key,
        },
    )
