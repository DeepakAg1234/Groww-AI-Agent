"""Local page over a week's artifacts. Jobs use the fake model and MCP client."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from groww_pulse.web.app import create_app
from tests.test_run import (
    DROPPED,
    KEPT,
    WEEK,
    ScriptedModel,
    _RecordingMcp,
    _project,
)


@pytest.fixture(autouse=True)
def _no_live_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("live client")

    monkeypatch.setattr("urllib.request.urlopen", blocked)
    monkeypatch.setattr("groww_pulse.pipeline.LiveModelClient.from_env", blocked)
    monkeypatch.setattr("groww_pulse.pipeline.LiveMcpClient.from_env", blocked)


def test_page_lists_weeks_and_reads_a_saved_run(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    _write_manifest(runs / "2026-W32", "ready")
    _write_manifest(runs / "2026-W41", "published")
    _write_review(runs / "2026-W41", rating=1)
    _write_review(runs / "2026-W41", rating=5)
    (runs / "2026-W41" / "pulse.txt").write_text("Weekly note\n", encoding="utf-8")
    (runs / "2026-W41" / "catalog.json").write_text(
        json.dumps(
            {
                "themes": [
                    {"id": "charts", "name": "Charts", "review_count": 2},
                    {"id": "support", "name": "Support", "review_count": 1},
                ],
                "top_theme_ids": ["charts"],
            }
        ),
        encoding="utf-8",
    )
    config = tmp_path / "config.yaml"
    config.write_text("product_name: Groww\nwindow_weeks: 10\napps: []\n", encoding="utf-8")
    client = TestClient(create_app(config, runs_dir=runs))

    page = client.get("/")
    listed = client.get("/api/weeks")
    missing = client.get("/api/weeks/2026-W01")
    saved = client.get("/api/weeks/2026-W41")
    invalid = client.get("/api/weeks/not-a-week")

    assert page.status_code == 200
    assert "Run this week" in page.text
    assert "Publish saved note" in page.text
    assert listed.json()["weeks"] == [
        {"week": "2026-W41", "status": "published"},
        {"week": "2026-W32", "status": "ready"},
    ]
    assert missing.status_code == 200
    assert missing.json()["manifest"] is None
    assert missing.json()["kept_count"] == 0
    assert missing.json()["ratings"] == {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    assert saved.json()["manifest"]["status"] == "published"
    assert saved.json()["catalog"]["themes"][0]["id"] == "charts"
    assert saved.json()["note"] == "Weekly note\n"
    assert saved.json()["kept_count"] == 2
    assert saved.json()["ratings"]["1"] == 1
    assert saved.json()["ratings"]["5"] == 1
    assert invalid.status_code == 400
    assert listed.headers["cache-control"] == "no-store"
    assert saved.headers["cache-control"] == "no-store"


def test_run_publishes_a_ready_fixture_and_a_later_publish_skips_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PULSE_DOC_ID", "1AbCdEfGhIjKlMnOpQrSt")
    monkeypatch.setenv("PULSE_EMAIL_TO", "ada@example.com")
    config, _export = _project(tmp_path, "play.csv", KEPT + DROPPED)
    run_dir = tmp_path / "data" / "runs" / WEEK
    model = ScriptedModel("ready", run_dir)
    mcp = _CountingMcp()
    client = TestClient(create_app(config, model_client=model, mcp_client=mcp))

    started = client.post(f"/api/weeks/{WEEK}/run")
    assert started.status_code == 202
    job = _wait(client, WEEK)

    assert job["state"] == "finished"
    assert job["status"] == "published"
    assert job["error"] == ""
    assert mcp.calls > 0
    assert (run_dir / "manifest.json").is_file()
    saved = client.get(f"/api/weeks/{WEEK}").json()
    assert saved["manifest"]["status"] == "published"
    assert saved["kept_count"] == 7
    assert saved["ratings"] == {"1": 3, "2": 1, "3": 1, "4": 0, "5": 2}
    prompts = len(model.prompts)

    published = client.post(f"/api/weeks/{WEEK}/publish")

    assert published.status_code == 409
    assert len(model.prompts) == prompts


def test_publish_saved_note_does_not_call_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PULSE_DOC_ID", "1AbCdEfGhIjKlMnOpQrSt")
    monkeypatch.setenv("PULSE_EMAIL_TO", "ada@example.com")
    config, _export = _project(tmp_path, "play.csv", KEPT + DROPPED)
    run_dir = tmp_path / "data" / "runs" / WEEK
    from groww_pulse.pipeline import run_week

    run_week(config, WEEK, client=ScriptedModel("ready", run_dir))
    model = _RaisingModel()
    client = TestClient(create_app(config, model_client=model, mcp_client=_CountingMcp()))

    published = client.post(f"/api/weeks/{WEEK}/publish")

    assert published.status_code == 200
    assert published.json()["manifest"]["status"] == "published"
    assert model.calls == 0


def test_run_skips_publish_when_the_window_has_no_reviews(tmp_path: Path) -> None:
    config, _export = _project(tmp_path, "empty.csv", [])
    model = _RaisingModel()
    mcp = _CountingMcp()
    client = TestClient(create_app(config, model_client=model, mcp_client=mcp))

    assert client.post(f"/api/weeks/{WEEK}/run").status_code == 202
    job = _wait(client, WEEK)

    assert job["status"] == "no_reviews"
    assert model.calls == 0
    assert mcp.calls == 0
    assert client.get(f"/api/weeks/{WEEK}").json()["manifest"]["status"] == "no_reviews"


def test_writes_require_the_bearer_token_when_it_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PULSE_API_TOKEN", "secret-token")
    config, _export = _project(tmp_path, "empty.csv", [])
    client = TestClient(create_app(config, model_client=_RaisingModel(), mcp_client=_CountingMcp()))
    headers = {"Authorization": "Bearer secret-token"}

    assert client.get("/api/weeks").status_code == 200
    missing = client.post(f"/api/weeks/{WEEK}/run")
    wrong = client.post(f"/api/weeks/{WEEK}/run", headers={"Authorization": "Bearer other"})
    started = client.post(f"/api/weeks/{WEEK}/run", headers=headers)

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.headers["cache-control"] == "no-store"
    assert started.status_code == 202
    job = _wait(client, WEEK)
    assert job["status"] == "no_reviews"

    denied = client.post(f"/api/weeks/{WEEK}/publish")
    assert denied.status_code == 401


def test_second_start_is_rejected_while_the_job_is_running(tmp_path: Path) -> None:
    config, _export = _project(tmp_path, "play.csv", KEPT + DROPPED)
    run_dir = tmp_path / "data" / "runs" / WEEK
    started = threading.Event()
    release = threading.Event()
    model = _Gate(ScriptedModel("ready", run_dir), started, release)
    client = TestClient(create_app(config, model_client=model, mcp_client=_CountingMcp()))

    first = client.post(f"/api/weeks/{WEEK}/run")
    assert first.status_code == 202
    assert started.wait(timeout=5)
    try:
        second = client.post(f"/api/weeks/{WEEK}/run")
        assert second.status_code == 409
        assert "already in progress" in second.json()["detail"]
    finally:
        release.set()
    job = _wait(client, WEEK)
    assert job["state"] == "finished"


def _wait(client: TestClient, week: str) -> dict[str, str]:
    for _ in range(100):
        job = client.get(f"/api/weeks/{week}/job").json()
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _write_manifest(folder: Path, status: str) -> None:
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(
        json.dumps({"week": folder.name, "status": status}),
        encoding="utf-8",
    )


def _write_review(folder: Path, *, rating: int) -> None:
    with (folder / "reviews.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "id": f"r{rating}",
                    "store": "play",
                    "app_id": "com.nextbillion.groww",
                    "rating": rating,
                    "title": "",
                    "text": "A kept review with enough words for the rating split.",
                    "created_at": "2026-09-01",
                    "theme_id": "",
                }
            )
            + "\n"
        )


class _RaisingModel:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        raise AssertionError(prompt)


class _CountingMcp(_RecordingMcp):
    def __init__(self) -> None:
        self.calls = 0

    def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        self.calls += 1
        return super().call_tool(name, arguments)


class _Gate:
    def __init__(
        self, inner: ScriptedModel, started: threading.Event, release: threading.Event
    ) -> None:
        self.inner = inner
        self.started = started
        self.release = release
        self.opened = False

    def complete(self, prompt: str) -> str:
        if not self.opened:
            self.opened = True
            self.started.set()
            assert self.release.wait(timeout=5)
        return self.inner.complete(prompt)
