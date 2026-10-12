"""Publish a saved pulse: authorize, append the Doc, then send mail."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from groww_pulse.config import Config
from groww_pulse.publish.docs import APPEND_TOOL, append_pulse
from groww_pulse.publish.gmail import SEND_TOOL, send_pulse
from groww_pulse.publish.mcp import McpClient, McpError

STATUS_PUBLISHED = "published"
STATUS_PUBLISH_FAILED = "publish_failed"

_PUBLISHABLE = frozenset({"ready", STATUS_PUBLISH_FAILED, STATUS_PUBLISHED})
_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ATTEMPTS = 3
_AUTH_CAPABILITIES = ["docs.append", "gmail.send"]


@dataclass(frozen=True)
class PublishUpdate:
    status: str
    reason: str
    doc_url: str
    message_id: str
    authorization_url: str


def publish_saved_run(
    config: Config,
    week: str,
    week_dir: Path,
    client: McpClient,
    *,
    manifest: dict[str, object],
) -> PublishUpdate:
    """Append ``pulse.txt`` and send it. Does not recluster or reread the export.

    A missing document id or recipient stops before any tool call. The same
    note reuses both idempotency keys. A changed note uses new keys.
    """
    kept_url = str(manifest.get("doc_url", ""))
    kept_message = str(manifest.get("message_id", ""))
    status = str(manifest.get("status", ""))
    if status not in _PUBLISHABLE:
        return _failed(
            f"run is not ready to publish: {status or 'missing'}",
            doc_url=kept_url,
            message_id=kept_message,
        )

    note_path = week_dir / "pulse.txt"
    pulse_path = week_dir / "pulse.json"
    if not note_path.is_file() or not pulse_path.is_file():
        return _failed(
            "pulse.txt and pulse.json are required",
            doc_url=kept_url,
            message_id=kept_message,
        )
    note = note_path.read_text(encoding="utf-8")
    if not note.strip():
        return _failed("pulse.txt is empty", doc_url=kept_url, message_id=kept_message)

    document_id = os.environ.get("PULSE_DOC_ID", "").strip() or config.doc_id.strip()
    if not document_id:
        return _failed("document id is missing", doc_url=kept_url, message_id=kept_message)
    recipient = os.environ.get("PULSE_EMAIL_TO", "").strip() or config.email_to.strip()
    if not recipient:
        return _failed("email_to is empty", doc_url=kept_url, message_id=kept_message)

    accounts = _call(lambda: client.call_tool("google_list_accounts", {}))
    if not _ok(accounts):
        return _failed(_reason(accounts), doc_url=kept_url, message_id=kept_message)
    account_id = _default_account_id(accounts)
    if not account_id:
        return _needs_authorization(client, account_id="", doc_url=kept_url, message_id=kept_message)

    auth = _call(
        lambda: client.call_tool(
            "google_authorization_status",
            {"account_id": account_id, "capability": "all"},
        )
    )
    if not _ok(auth):
        return _failed(_reason(auth), doc_url=kept_url, message_id=kept_message)
    auth_result = auth.get("result")
    authorized = isinstance(auth_result, dict) and auth_result.get("authorized") is True
    if not authorized:
        return _needs_authorization(
            client,
            account_id=account_id,
            doc_url=kept_url,
            message_id=kept_message,
        )

    doc_key = idempotency_key(week, "doc", note)
    appended = _call(
        lambda: append_pulse(
            client,
            account_id=account_id,
            document_id=document_id,
            content=note,
            idempotency_key=doc_key,
        )
    )
    if not _ok(appended):
        return _failed(_reason(appended), doc_url=kept_url, message_id=kept_message)
    append_result = appended.get("result")
    if not isinstance(append_result, dict) or append_result.get("status") != "appended":
        return _failed(
            f"{APPEND_TOOL}: append did not report status appended",
            doc_url=kept_url,
            message_id=kept_message,
        )
    raw_id = str(append_result.get("document_id", "")).strip()
    if not raw_id:
        return _failed(
            f"{APPEND_TOOL}: document_id missing",
            doc_url=kept_url,
            message_id=kept_message,
        )
    doc_url = f"https://docs.google.com/document/d/{raw_id}/edit"
    _write_pulse_fields(pulse_path, doc_url=doc_url, message_id=kept_message)

    subject = f"Groww weekly pulse — {week}"
    mail_body = _mail_body(note, doc_url)
    mail_key = idempotency_key(week, "mail", mail_body)
    sent = _call(
        lambda: send_pulse(
            client,
            account_id=account_id,
            to=recipient,
            subject=subject,
            body=mail_body,
            idempotency_key=mail_key,
        )
    )
    if not _ok(sent):
        return _failed(_reason(sent), doc_url=doc_url, message_id=kept_message)
    send_result = sent.get("result")
    if not isinstance(send_result, dict) or send_result.get("status") != "sent":
        return _failed(
            f"{SEND_TOOL}: send did not report status sent",
            doc_url=doc_url,
            message_id=kept_message,
        )
    message_id = str(send_result.get("message_id", "")).strip()
    if not message_id:
        return _failed(
            f"{SEND_TOOL}: message_id missing",
            doc_url=doc_url,
            message_id=kept_message,
        )
    _write_pulse_fields(pulse_path, doc_url=doc_url, message_id=message_id)
    return PublishUpdate(
        status=STATUS_PUBLISHED,
        reason="",
        doc_url=doc_url,
        message_id=message_id,
        authorization_url="",
    )


def idempotency_key(week: str, kind: str, body: str) -> str:
    """Stable key for one week and one body. A changed body gets a new key."""
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    key = f"pulse-{week}-{kind}-{digest}"
    if _KEY_RE.fullmatch(key) is None:
        raise ValueError(f"idempotency key is not valid: {key}")
    return key


def _needs_authorization(
    client: McpClient,
    *,
    account_id: str,
    doc_url: str,
    message_id: str,
) -> PublishUpdate:
    arguments: dict[str, object] = {"capabilities": list(_AUTH_CAPABILITIES)}
    if account_id:
        arguments["account_id"] = account_id
    started = _call(lambda: client.call_tool("google_begin_authorization", arguments))
    url = ""
    if _ok(started):
        result = started.get("result")
        if isinstance(result, dict):
            url = str(result.get("authorization_url", "")).strip()
        reason = "authorization required"
    else:
        reason = _reason(started)
    return PublishUpdate(
        status=STATUS_PUBLISH_FAILED,
        reason=reason,
        doc_url=doc_url,
        message_id=message_id,
        authorization_url=url,
    )


def _call(operation: Callable[[], dict[str, object]]) -> dict[str, object]:
    """Call until success, or stop when the envelope is not retryable."""
    envelope: dict[str, object] = {}
    for _attempt in range(_ATTEMPTS):
        try:
            envelope = operation()
        except McpError as exc:
            return {
                "ok": False,
                "tool": "",
                "error": {"code": "TRANSPORT", "message": str(exc), "retryable": False},
            }
        if _ok(envelope):
            return envelope
        error = envelope.get("error")
        if not isinstance(error, dict) or error.get("retryable") is not True:
            return envelope
    return envelope


def _ok(envelope: dict[str, object]) -> bool:
    return envelope.get("ok") is True


def _reason(envelope: dict[str, object]) -> str:
    tool = str(envelope.get("tool", "")).strip()
    error = envelope.get("error")
    if not isinstance(error, dict):
        return tool or "publish failed"
    code = str(error.get("code", "")).strip()
    message = str(error.get("message", "")).strip()
    parts = [part for part in (tool, code, message) if part]
    return ": ".join(parts) if parts else "publish failed"


def _default_account_id(envelope: dict[str, object]) -> str:
    result = envelope.get("result")
    if not isinstance(result, dict):
        return ""
    accounts = result.get("accounts")
    if not isinstance(accounts, list):
        return ""
    for account in accounts:
        if not isinstance(account, dict):
            continue
        if account.get("status") == "active" and account.get("default") is True:
            account_id = str(account.get("account_id", "")).strip()
            if account_id:
                return account_id
    return ""


def _mail_body(note: str, doc_url: str) -> str:
    text = note if note.endswith("\n") else note + "\n"
    return f"{text}\n{doc_url}\n"


def _write_pulse_fields(path: Path, *, doc_url: str, message_id: str) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("pulse.json must be an object")
    raw["doc_url"] = doc_url
    raw["message_id"] = message_id
    path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _failed(reason: str, *, doc_url: str, message_id: str) -> PublishUpdate:
    return PublishUpdate(
        status=STATUS_PUBLISH_FAILED,
        reason=reason,
        doc_url=doc_url,
        message_id=message_id,
        authorization_url="",
    )
