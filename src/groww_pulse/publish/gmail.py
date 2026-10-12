"""Send a pulse note through ``gmail_send_email``.

This server has no draft tool. Attachments are omitted because the schema
rejects them.
"""

from __future__ import annotations

from groww_pulse.publish.mcp import McpClient

SEND_TOOL = "gmail_send_email"


def send_pulse(
    client: McpClient,
    *,
    account_id: str,
    to: str,
    subject: str,
    body: str,
    idempotency_key: str,
) -> dict[str, object]:
    """Send one plain-text message. ``cc``, ``bcc``, and ``attachments`` are omitted."""
    return client.call_tool(
        SEND_TOOL,
        {
            "to": [to],
            "subject": subject,
            "body": body,
            "account_id": account_id,
            "body_format": "plain",
            "idempotency_key": idempotency_key,
        },
    )
