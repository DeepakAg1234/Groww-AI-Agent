"""Local weekly run with a fake model. Publish uses a fake MCP client."""

import csv
import json
from pathlib import Path

import pytest

from groww_pulse.__main__ import main
from groww_pulse.checks import load_reviews_jsonl
from groww_pulse.pipeline import (
    STATUS_FAILED,
    STATUS_NO_REVIEWS,
    STATUS_READY,
    STATUS_SHORT_CATALOG,
    run_week,
)

WEEK = "2026-W41"
APP = "com.nextbillion.groww"

KEPT = [
    (1, "The order delay and the order failed this morning again today.", "2026-09-01"),
    (2, "Another order delay showed up when the order failed after lunch.", "2026-09-02"),
    (1, "The chart lag and the slow chart made the open unusable today.", "2026-09-03"),
    (5, "A slow chart still loaded after the chart lag during the close.", "2026-09-04"),
    (1, "Customer support and the call centre never answered my request today.", "2026-09-05"),
    (3, "The call centre and customer support left the ticket open all day.", "2026-09-06"),
    (5, "The brokerage fee is high but this is still a nice app overall.", "2026-09-07"),
]
DROPPED = [
    (4, "Too short", "2026-09-08"),
    (4, "This older review sits well outside the configured window today.", "2026-01-15"),
]

CATALOG = {
    "themes": [
        {
            "id": "orders",
            "name": "Orders",
            "definition": "Order placement and failed orders.",
            "examples": ["order delay", "order failed"],
        },
        {
            "id": "charts",
            "name": "Charts",
            "definition": "Chart speed and lag.",
            "examples": ["chart lag", "slow chart"],
        },
        {
            "id": "support",
            "name": "Support",
            "definition": "Customer support and the call centre.",
            "examples": ["customer support", "call centre"],
        },
        {
            "id": "charges",
            "name": "Charges",
            "definition": "Brokerage and other charges.",
            "examples": ["brokerage fee", "nice app"],
        },
    ]
}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network call")

    monkeypatch.setattr("urllib.request.urlopen", blocked)


class ScriptedModel:
    def __init__(self, mode: str, run_dir: Path) -> None:
        self.mode = mode
        self.run_dir = run_dir
        self.prompts: list[str] = []
        self.blank_theme_ids: list[bool] = []

    def complete(self, prompt: str) -> str:
        if prompt.startswith("TASK: catalog") and "rejected" not in prompt:
            reviews = load_reviews_jsonl(self.run_dir / "reviews.jsonl")
            self.blank_theme_ids.append(all(review.theme_id == "" for review in reviews))
        self.prompts.append(prompt)
        task = prompt.splitlines()[0]
        if task == "TASK: catalog":
            return json.dumps(CATALOG)
        if task == "TASK: assign":
            labeler = _label_short if self.mode == "short" else _label_ready
            return _assignments(prompt, labeler)
        if task == "TASK: compose":
            pools = _pools(prompt)
            assert all(len(rows) <= 12 for rows in pools.values())
            if self.mode == "badquote":
                return _compose(pools, bad_quote=True)
            return _compose(pools)
        raise AssertionError(task)


def test_fixture_run_is_ready_and_a_rerun_replaces_the_folder(tmp_path: Path) -> None:
    for _rating, text, _date in KEPT:
        assert len(text.split()) > 8
    config, export = _project(tmp_path, "play.csv", KEPT + DROPPED)
    original = export.read_bytes()
    run_dir = tmp_path / "data" / "runs" / WEEK
    model = ScriptedModel("ready", run_dir)

    result = run_week(config, WEEK, client=model)

    manifest = _manifest(result.manifest_path)
    assert result.status == STATUS_READY
    assert manifest["status"] == STATUS_READY
    assert manifest["week"] == WEEK
    assert manifest["window"] == {"start": "2026-08-03", "end": "2026-10-11"}
    assert manifest["rows_read"] == 9
    assert manifest["rows_kept"] == 7
    assert manifest["rows_dropped"] == 2
    assert manifest["empty_exports"] == []
    assert manifest["unclassified_count"] == 1
    assert manifest["top_theme_ids"] == ["orders", "support", "charts"]
    assert manifest["doc_url"] == ""
    assert manifest["draft_id"] == ""
    assert manifest["checks_ran"] is True
    assert manifest["check_results"]
    assert all(item["passed"] for item in manifest["check_results"])
    counts = {theme["id"]: theme["review_count"] for theme in manifest["themes"]}
    assert counts == {"orders": 2, "charts": 2, "support": 2, "charges": 0}
    assert len(manifest["themes"]) <= 5
    assert [prompt.splitlines()[0] for prompt in model.prompts] == [
        "TASK: catalog",
        "TASK: assign",
        "TASK: compose",
    ]
    assert model.blank_theme_ids == [True]
    artifacts = {
        "reviews.jsonl",
        "catalog.json",
        "pulse.json",
        "pulse.txt",
        "manifest.json",
    }
    assert {path.name for path in run_dir.iterdir()} == artifacts
    pulse = json.loads((run_dir / "pulse.json").read_text(encoding="utf-8"))
    assert pulse["word_count"] <= 280
    assert pulse["doc_url"] == ""
    assert pulse["draft_id"] == ""
    assert pulse["top_theme_ids"] == ["orders", "support", "charts"]
    first_ids = [review.id for review in load_reviews_jsonl(run_dir / "reviews.jsonl")]
    assert len(first_ids) == 7
    assert any(review.theme_id == "unclassified" for review in load_reviews_jsonl(run_dir / "reviews.jsonl"))

    (run_dir / "obsolete.txt").write_text("stale", encoding="utf-8")
    again = ScriptedModel("ready", run_dir)
    second = run_week(config, WEEK, client=again)

    assert second.status == STATUS_READY
    assert not (run_dir / "obsolete.txt").exists()
    assert {path.name for path in run_dir.iterdir()} == artifacts
    assert again.blank_theme_ids == [True]
    second_ids = [review.id for review in load_reviews_jsonl(run_dir / "reviews.jsonl")]
    assert second_ids == first_ids
    assert export.read_bytes() == original


def test_short_catalog_stops_before_compose(tmp_path: Path) -> None:
    config, _export = _project(tmp_path, "play.csv", KEPT + DROPPED)
    run_dir = tmp_path / "data" / "runs" / WEEK
    model = ScriptedModel("short", run_dir)

    result = run_week(config, WEEK, client=model)

    manifest = _manifest(result.manifest_path)
    assert result.status == STATUS_SHORT_CATALOG
    assert manifest["unclassified_count"] == 5
    assert manifest["checks_ran"] is False
    counts = {theme["id"]: theme["review_count"] for theme in manifest["themes"]}
    assert counts["orders"] == 2
    assert sum(1 for count in counts.values() if count) < 3
    assert "TASK: compose" not in [prompt.splitlines()[0] for prompt in model.prompts]
    assert (run_dir / "catalog.json").is_file()
    assert (run_dir / "reviews.jsonl").is_file()
    assert not (run_dir / "pulse.json").exists()
    assert not (run_dir / "pulse.txt").exists()


def test_empty_export_stops_before_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _export = _project(
        tmp_path,
        "empty.csv",
        [(4, "This older review sits well outside the configured window today.", "2026-01-15")],
    )
    calls = {"count": 0}

    def from_env() -> ScriptedModel:
        calls["count"] += 1
        raise AssertionError("live client")

    monkeypatch.setattr("groww_pulse.pipeline.LiveModelClient.from_env", from_env)
    result = run_week(config, WEEK)

    manifest = _manifest(result.manifest_path)
    assert result.status == STATUS_NO_REVIEWS
    assert calls["count"] == 0
    assert manifest["rows_read"] == 1
    assert manifest["rows_kept"] == 0
    assert manifest["empty_exports"] == ["empty.csv"]
    assert "empty.csv" in manifest["reason"]
    assert manifest["themes"] == []
    run_dir = result.run_dir
    assert (run_dir / "reviews.jsonl").is_file()
    assert load_reviews_jsonl(run_dir / "reviews.jsonl") == []
    assert not (run_dir / "catalog.json").exists()
    assert not (run_dir / "pulse.json").exists()


def test_bad_quote_is_not_ready(tmp_path: Path) -> None:
    config, _export = _project(tmp_path, "play.csv", KEPT + DROPPED)
    run_dir = tmp_path / "data" / "runs" / WEEK
    model = ScriptedModel("badquote", run_dir)

    result = run_week(config, WEEK, client=model)

    manifest = _manifest(result.manifest_path)
    assert result.status == STATUS_FAILED
    assert manifest["status"] == STATUS_FAILED
    assert manifest["doc_url"] == ""
    assert manifest["draft_id"] == ""
    verbatim = next(item for item in manifest["check_results"] if item["rule"] == "verbatim")
    assert verbatim["passed"] is False
    pulse = json.loads((run_dir / "pulse.json").read_text(encoding="utf-8"))
    assert pulse["doc_url"] == ""
    assert pulse["draft_id"] == ""
    assert [prompt.splitlines()[0] for prompt in model.prompts].count("TASK: compose") == 3


def test_cli_run_prints_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config, _export = _project(tmp_path, "play.csv", KEPT + DROPPED)
    run_dir = tmp_path / "data" / "runs" / WEEK
    model = ScriptedModel("ready", run_dir)
    monkeypatch.setattr("groww_pulse.pipeline.LiveModelClient.from_env", lambda: model)

    assert main(["run", "--week", WEEK, "--config", str(config)]) == 0

    assert capsys.readouterr().out.strip() == STATUS_READY
    assert _manifest(run_dir / "manifest.json")["rows_kept"] == 7
    assert model.prompts


def test_cli_publish_does_not_recluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config, _export = _project(tmp_path, "play.csv", KEPT + DROPPED)
    run_dir = tmp_path / "data" / "runs" / WEEK
    run_week(config, WEEK, client=ScriptedModel("ready", run_dir))
    before = _files(run_dir)

    def banned(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("publish must not rebuild the run")

    monkeypatch.setattr("groww_pulse.pipeline.cluster_directory", banned)
    monkeypatch.setattr("groww_pulse.pipeline.compose_directory", banned)
    monkeypatch.setattr("groww_pulse.pipeline.LiveModelClient.from_env", banned)
    monkeypatch.setattr("groww_pulse.pipeline.write_clean_reviews", banned)
    monkeypatch.setenv("PULSE_DOC_ID", "1AbCdEfGhIjKlMnOpQrSt")
    monkeypatch.setenv("PULSE_EMAIL_TO", "ada@example.com")
    monkeypatch.setattr(
        "groww_pulse.pipeline.LiveMcpClient.from_env",
        lambda: _RecordingMcp(),
    )
    reviews = (run_dir / "reviews.jsonl").read_bytes()
    catalog = (run_dir / "catalog.json").read_bytes()

    assert main(["publish", "--week", WEEK, "--config", str(config)]) == 0

    captured = capsys.readouterr()
    assert captured.out.strip() == "published"
    assert captured.err == ""
    assert (run_dir / "reviews.jsonl").read_bytes() == reviews
    assert (run_dir / "catalog.json").read_bytes() == catalog
    assert _files(run_dir).keys() == before.keys()


def test_cli_publish_missing_folder(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config, _export = _project(tmp_path, "empty.csv", [])
    assert main(["publish", "--week", "2026-W01", "--config", str(config)]) == 1
    assert "no run folder" in capsys.readouterr().err


def _project(
    tmp_path: Path, export_name: str, rows: list[tuple[int, str, str]]
) -> tuple[Path, Path]:
    export = tmp_path / export_name
    with export.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["rating", "text", "date", "title"])
        for rating, text, created_at in rows:
            writer.writerow([rating, text, created_at, ""])
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                "product_name: Groww",
                "window_weeks: 10",
                "apps:",
                "  - store: play",
                f"    app_id: {APP}",
                f"    export: {export_name}",
                'email_to: ""',
                'doc_title_pattern: "Groww weekly pulse — {week}"',
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
    return config, export


def _label_ready(text: str) -> str:
    folded = text.casefold()
    if "order delay" in folded:
        return "orders"
    if "chart lag" in folded:
        return "charts"
    if "call centre" in folded:
        return "support"
    return "unclassified"


def _label_short(text: str) -> str:
    if "order delay" in text.casefold():
        return "orders"
    return "unclassified"


def _assignments(prompt: str, labeler) -> str:
    body = prompt.split("Reviews:\n", 1)[1]
    pairs = []
    for line in body.splitlines():
        if not line.startswith("{"):
            break
        item = json.loads(line)
        pairs.append({"id": item["id"], "theme_id": labeler(item["text"])})
    return json.dumps({"assignments": pairs})


def _pools(prompt: str) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for section in prompt.split("--- theme ")[1:]:
        theme_id, rest = section.split(" ---", 1)
        body = rest.split("reviews:\n", 1)[1]
        rows = []
        for line in body.splitlines():
            if not line.startswith("{"):
                break
            rows.append(json.loads(line))
        found[theme_id] = rows
    return found


def _compose(pools: dict[str, list[dict]], *, bad_quote: bool = False) -> str:
    quotes = []
    for rows in pools.values():
        assert rows
        review = rows[0]
        text = "this quote is not in the review" if bad_quote and not quotes else review["text"]
        quotes.append({"review_id": review["id"], "text": text})
    return json.dumps(
        {
            "summaries": [
                {"theme_id": "orders", "text": "Users want an order to show the price they confirmed."},
                {"theme_id": "charts", "text": "Users want charts to open without a lag."},
                {"theme_id": "support", "text": "Users want support to stay on the call."},
            ],
            "quotes": quotes,
            "actions": [
                {
                    "theme_id": "orders",
                    "text": "Fix the Orders screen so a failed order shows the confirmed price.",
                },
                {
                    "theme_id": "charts",
                    "text": "Make Charts open without the lag users hit at the open.",
                },
                {
                    "theme_id": "support",
                    "text": "Answer Support calls instead of disconnecting the user.",
                },
            ],
        }
    )


def _manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class _RecordingMcp:
    """Enough of a successful publish for the CLI to finish without the network."""

    def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        if name == "google_list_accounts":
            return {
                "ok": True,
                "tool": name,
                "result": {
                    "accounts": [
                        {
                            "account_id": "acct_AAAAAAAAAAAAAAAAAAAAAA",
                            "status": "active",
                            "default": True,
                        }
                    ]
                },
                "warnings": [],
            }
        if name == "google_authorization_status":
            return {
                "ok": True,
                "tool": name,
                "result": {"authorized": True, "missing_scopes": []},
                "warnings": [],
            }
        if name == "gdocs_append_content":
            return {
                "ok": True,
                "tool": name,
                "result": {
                    "document_id": "1AbCdEfGhIjKlMnOpQrSt",
                    "status": "appended",
                    "document_title": "Pulse",
                },
                "warnings": [],
            }
        if name == "gmail_send_email":
            return {
                "ok": True,
                "tool": name,
                "result": {"message_id": "18c0", "status": "sent"},
                "warnings": [],
            }
        raise AssertionError(name)


def _files(run_dir: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()}
