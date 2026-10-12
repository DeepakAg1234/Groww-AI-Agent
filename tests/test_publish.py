"""Publish a saved pulse through a fake MCP client. No live network calls."""

import json
from pathlib import Path

import pytest

from groww_pulse.pipeline import publish_week
from groww_pulse.publish.mcp import (
    DEFAULT_MCP_URL,
    LiveMcpClient,
    envelope_from_tool_result,
)
from groww_pulse.publish.publisher import idempotency_key

WEEK = "2026-W41"
DOC_ID = "1AbCdEfGhIjKlMnOpQrSt"
ACCOUNT = "acct_AAAAAAAAAAAAAAAAAAAAAA"
NOTE = "Orders are slow and charts lag.\nSupport did not answer.\n"
EMAIL = "ada@example.com"


class FakeMcp:
    def __init__(self, scripts: dict[str, list[dict[str, object]]]) -> None:
        self.scripts = scripts
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append((name, arguments))
        queue = self.scripts.get(name)
        if not queue:
            raise AssertionError(f"unexpected tool {name}")
        return queue.pop(0)


def test_publish_appends_then_sends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, week_dir = _ready_run(tmp_path)
    monkeypatch.delenv("PULSE_DOC_ID", raising=False)
    monkeypatch.delenv("PULSE_EMAIL_TO", raising=False)
    client = FakeMcp(_success_scripts())

    result = publish_week(config, WEEK, client=client)

    manifest = _manifest(week_dir)
    pulse = json.loads((week_dir / "pulse.json").read_text(encoding="utf-8"))
    names = [name for name, _arguments in client.calls]
    assert result.status == "published"
    assert names == [
        "google_list_accounts",
        "google_authorization_status",
        "gdocs_append_content",
        "gmail_send_email",
    ]
    assert client.calls[0][1] == {}
    assert client.calls[1][1] == {"account_id": ACCOUNT, "capability": "all"}
    append = client.calls[2][1]
    assert append["document_id"] == "https://docs.google.com/document/d/EXISTING/edit"
    assert append["content"] == NOTE
    assert append["format"] == "plain_text"
    assert append["prepend_newline"] is True
    assert append["account_id"] == ACCOUNT
    assert append["idempotency_key"] == idempotency_key(WEEK, "doc", NOTE)
    assert "title" not in append
    send = client.calls[3][1]
    assert send["to"] == [EMAIL]
    assert send["subject"] == f"Groww weekly pulse — {WEEK}"
    assert send["body_format"] == "plain"
    assert NOTE.strip() in str(send["body"])
    assert manifest["doc_url"] in str(send["body"])
    assert "cc" not in send and "bcc" not in send and "attachments" not in send
    assert send["idempotency_key"] == idempotency_key(WEEK, "mail", str(send["body"]))
    assert manifest["status"] == "published"
    assert manifest["doc_url"] == f"https://docs.google.com/document/d/{DOC_ID}/edit"
    assert manifest["message_id"] == "18c0"
    assert manifest["authorization_url"] == ""
    assert manifest["rows_kept"] == 7
    assert pulse["doc_url"] == manifest["doc_url"]
    assert pulse["message_id"] == "18c0"
    assert "reviews.jsonl" not in str(send["body"])


def test_same_note_reuses_idempotency_keys(tmp_path: Path) -> None:
    config, _week_dir = _ready_run(tmp_path)
    first = FakeMcp(_success_scripts())
    second = FakeMcp(_success_scripts())

    publish_week(config, WEEK, client=first)
    publish_week(config, WEEK, client=second)

    assert _keys(first) == _keys(second)


def test_changed_note_uses_new_keys(tmp_path: Path) -> None:
    config, week_dir = _ready_run(tmp_path)
    first = FakeMcp(_success_scripts())
    publish_week(config, WEEK, client=first)
    (week_dir / "pulse.txt").write_text(NOTE + "A second sentence about fees.\n", encoding="utf-8")
    second = FakeMcp(_success_scripts())
    publish_week(config, WEEK, client=second)

    assert _keys(first) != _keys(second)
    assert publish_week(config, WEEK, client=FakeMcp(_success_scripts())).status == "published"


def test_missing_document_id_makes_no_tool_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, week_dir = _ready_run(tmp_path, doc_id="")
    monkeypatch.delenv("PULSE_DOC_ID", raising=False)
    note = (week_dir / "pulse.txt").read_bytes()
    client = FakeMcp(_success_scripts())

    result = publish_week(config, WEEK, client=client)

    manifest = _manifest(week_dir)
    assert result.status == "publish_failed"
    assert manifest["reason"] == "document id is missing"
    assert client.calls == []
    assert (week_dir / "pulse.txt").read_bytes() == note
    assert json.loads((week_dir / "pulse.json").read_text(encoding="utf-8"))["doc_url"] == ""


def test_missing_account_records_authorization_url(tmp_path: Path) -> None:
    config, week_dir = _ready_run(tmp_path)
    client = FakeMcp(
        {
            "google_list_accounts": [_ok("google_list_accounts", {"accounts": []})],
            "google_begin_authorization": [
                _ok(
                    "google_begin_authorization",
                    {
                        "authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?client_id=example",
                        "expires_at": "2026-10-10T12:10:00.000Z",
                        "status": "pending",
                    },
                )
            ],
        }
    )

    result = publish_week(config, WEEK, client=client)

    manifest = _manifest(week_dir)
    assert result.status == "publish_failed"
    assert manifest["authorization_url"].startswith("https://accounts.google.com/")
    assert [name for name, _arguments in client.calls] == [
        "google_list_accounts",
        "google_begin_authorization",
    ]
    assert client.calls[1][1] == {"capabilities": ["docs.append", "gmail.send"]}


def test_unauthorized_account_does_not_append(tmp_path: Path) -> None:
    config, week_dir = _ready_run(tmp_path)
    client = FakeMcp(
        {
            "google_list_accounts": [_accounts()],
            "google_authorization_status": [
                _ok(
                    "google_authorization_status",
                    {
                        "account_id": ACCOUNT,
                        "capability": "all",
                        "authorized": False,
                        "missing_scopes": ["https://www.googleapis.com/auth/gmail.send"],
                    },
                )
            ],
            "google_begin_authorization": [
                _ok(
                    "google_begin_authorization",
                    {
                        "authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?client_id=example",
                        "status": "pending",
                    },
                )
            ],
        }
    )

    publish_week(config, WEEK, client=client)

    assert [name for name, _arguments in client.calls][-1] == "google_begin_authorization"
    assert client.calls[-1][1]["account_id"] == ACCOUNT
    assert "gdocs_append_content" not in [name for name, _arguments in client.calls]
    assert _manifest(week_dir)["status"] == "publish_failed"


def test_append_failure_does_not_send(tmp_path: Path) -> None:
    config, week_dir = _ready_run(tmp_path)
    client = FakeMcp(
        {
            "google_list_accounts": [_accounts()],
            "google_authorization_status": [_authorized()],
            "gdocs_append_content": [_error("gdocs_append_content", "ACCESS_DENIED", retryable=False)],
        }
    )

    result = publish_week(config, WEEK, client=client)

    assert result.status == "publish_failed"
    assert "ACCESS_DENIED" in _manifest(week_dir)["reason"]
    assert "gmail_send_email" not in [name for name, _arguments in client.calls]
    assert json.loads((week_dir / "pulse.json").read_text(encoding="utf-8"))["doc_url"] == ""


def test_send_failure_keeps_doc_url(tmp_path: Path) -> None:
    config, week_dir = _ready_run(tmp_path)
    scripts = _success_scripts()
    scripts["gmail_send_email"] = [_error("gmail_send_email", "RATE_LIMITED", retryable=False)]
    client = FakeMcp(scripts)

    result = publish_week(config, WEEK, client=client)

    manifest = _manifest(week_dir)
    pulse = json.loads((week_dir / "pulse.json").read_text(encoding="utf-8"))
    assert result.status == "publish_failed"
    assert manifest["doc_url"] == f"https://docs.google.com/document/d/{DOC_ID}/edit"
    assert manifest["message_id"] == ""
    assert pulse["doc_url"] == manifest["doc_url"]
    assert pulse["message_id"] == ""


def test_retryable_append_is_retried_once(tmp_path: Path) -> None:
    config, _week_dir = _ready_run(tmp_path)
    scripts = _success_scripts()
    scripts["gdocs_append_content"] = [
        _error("gdocs_append_content", "RATE_LIMITED", retryable=True),
        _appended(),
    ]
    client = FakeMcp(scripts)

    assert publish_week(config, WEEK, client=client).status == "published"
    appends = [call for call in client.calls if call[0] == "gdocs_append_content"]
    assert len(appends) == 2
    assert appends[0][1]["idempotency_key"] == appends[1][1]["idempotency_key"]


def test_idempotency_conflict_is_not_retried(tmp_path: Path) -> None:
    config, _week_dir = _ready_run(tmp_path)
    scripts = _success_scripts()
    scripts["gdocs_append_content"] = [
        _error("gdocs_append_content", "IDEMPOTENCY_CONFLICT", retryable=False)
    ]
    client = FakeMcp(scripts)

    publish_week(config, WEEK, client=client)

    assert [name for name, _arguments in client.calls].count("gdocs_append_content") == 1


def test_failed_run_is_not_published(tmp_path: Path) -> None:
    config, week_dir = _ready_run(tmp_path, status="failed")
    client = FakeMcp(_success_scripts())

    result = publish_week(config, WEEK, client=client)

    assert result.status == "publish_failed"
    assert client.calls == []
    assert "not ready" in _manifest(week_dir)["reason"]


def test_env_overrides_doc_and_recipient(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, _week_dir = _ready_run(tmp_path, doc_id="from-config", email="config@example.com")
    monkeypatch.setenv("PULSE_DOC_ID", "https://docs.google.com/document/d/FROMENV/edit")
    monkeypatch.setenv("PULSE_EMAIL_TO", "env@example.com")
    client = FakeMcp(_success_scripts())

    publish_week(config, WEEK, client=client)

    assert client.calls[2][1]["document_id"].endswith("/FROMENV/edit")
    assert client.calls[3][1]["to"] == ["env@example.com"]


def test_envelope_reads_structured_content_and_text() -> None:
    envelope = {
        "ok": True,
        "operation_id": "op_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "tool": "gmail_send_email",
        "result": {"message_id": "18c0", "status": "sent"},
        "warnings": [],
    }
    from_structured = envelope_from_tool_result(
        {
            "content": [{"type": "text", "text": json.dumps(envelope)}],
            "structuredContent": envelope,
            "isError": False,
        }
    )
    from_text = envelope_from_tool_result(
        {"content": [{"type": "text", "text": json.dumps(envelope)}], "isError": False}
    )
    failed = envelope_from_tool_result(
        {
            "content": [{"type": "text", "text": json.dumps({**envelope, "ok": True})}],
            "isError": True,
        }
    )
    assert from_structured["ok"] is True
    assert from_text["result"]["message_id"] == "18c0"
    assert failed["ok"] is False


def test_live_client_defaults_to_hosted_url_without_calling_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PULSE_MCP_URL", raising=False)
    monkeypatch.delenv("PULSE_MCP_TOKEN", raising=False)
    client = LiveMcpClient.from_env()
    assert client.url == DEFAULT_MCP_URL
    assert client.url == "https://mcp-server-production-af95.up.railway.app/mcp"


def _ready_run(
    tmp_path: Path,
    *,
    doc_id: str = "https://docs.google.com/document/d/EXISTING/edit",
    email: str = EMAIL,
    status: str = "ready",
) -> tuple[Path, Path]:
    week_dir = tmp_path / "data" / "runs" / WEEK
    week_dir.mkdir(parents=True)
    (week_dir / "pulse.txt").write_text(NOTE, encoding="utf-8")
    (week_dir / "pulse.json").write_text(
        json.dumps({"week": WEEK, "doc_url": "", "draft_id": "", "message_id": "", "word_count": 8})
        + "\n",
        encoding="utf-8",
    )
    (week_dir / "reviews.jsonl").write_text('{"id":"r1"}\n', encoding="utf-8")
    (week_dir / "manifest.json").write_text(
        json.dumps(
            {
                "week": WEEK,
                "status": status,
                "rows_kept": 7,
                "doc_url": "",
                "draft_id": "",
                "message_id": "",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                "product_name: Groww",
                "window_weeks: 10",
                "apps:",
                "  - store: play",
                "    app_id: com.nextbillion.groww",
                "    export: missing.csv",
                f'email_to: "{email}"',
                f'doc_id: "{doc_id}"',
                'doc_title_pattern: "not sent — {week}"',
                "max_themes: 5",
                "pulse_theme_count: 3",
                "quote_count: 3",
                "action_count: 3",
                "target_words: 250",
                "max_words: 280",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config, week_dir


def _success_scripts() -> dict[str, list[dict[str, object]]]:
    return {
        "google_list_accounts": [_accounts()],
        "google_authorization_status": [_authorized()],
        "gdocs_append_content": [_appended()],
        "gmail_send_email": [
            _ok(
                "gmail_send_email",
                {"message_id": "18c0", "status": "sent", "account_id": ACCOUNT, "recipient_count": 1},
                warnings=[{"code": "DELIVERY_NOT_GUARANTEED", "message": "Gmail accepted the message."}],
            )
        ],
    }


def _accounts() -> dict[str, object]:
    return _ok(
        "google_list_accounts",
        {
            "accounts": [
                {
                    "account_id": ACCOUNT,
                    "email": EMAIL,
                    "display_name": "Ada",
                    "granted_scopes": [],
                    "status": "active",
                    "default": True,
                }
            ]
        },
    )


def _authorized() -> dict[str, object]:
    return _ok(
        "google_authorization_status",
        {
            "account_id": ACCOUNT,
            "capability": "all",
            "authorized": True,
            "missing_scopes": [],
        },
    )


def _appended() -> dict[str, object]:
    return _ok(
        "gdocs_append_content",
        {
            "document_id": DOC_ID,
            "account_id": ACCOUNT,
            "status": "appended",
            "document_title": "Groww weekly pulse",
        },
    )


def _ok(
    tool: str,
    result: dict[str, object],
    warnings: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "ok": True,
        "operation_id": "op_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "tool": tool,
        "result": result,
        "warnings": warnings or [],
    }


def _error(tool: str, code: str, *, retryable: bool) -> dict[str, object]:
    return {
        "ok": False,
        "operation_id": "op_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "tool": tool,
        "error": {
            "code": code,
            "message": code,
            "retryable": retryable,
            "error_id": "op_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        },
    }


def _keys(client: FakeMcp) -> tuple[str, str]:
    by_name = {name: arguments for name, arguments in client.calls}
    return (
        str(by_name["gdocs_append_content"]["idempotency_key"]),
        str(by_name["gmail_send_email"]["idempotency_key"]),
    )


def _manifest(week_dir: Path) -> dict[str, object]:
    loaded = json.loads((week_dir / "manifest.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded
