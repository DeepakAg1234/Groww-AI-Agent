"""JSON API and the single local page over a week's run folder."""

from __future__ import annotations

import hmac
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from groww_pulse.checks import load_reviews_jsonl
from groww_pulse.pipeline import STATUS_READY
from groww_pulse.pipeline import publish_week as publish_saved_week
from groww_pulse.publish.publisher import STATUS_PUBLISH_FAILED
from groww_pulse.web.jobs import JobBusy, JobRunner
from groww_pulse.weeks import parse_iso_week

_STATIC = Path(__file__).resolve().parent / "static" / "index.html"
_PUBLISH_FROM = frozenset({STATUS_READY, STATUS_PUBLISH_FAILED})
_RATINGS = ("1", "2", "3", "4", "5")


def create_app(
    config_path: Path | str,
    *,
    runs_dir: Path | str | None = None,
    model_client: object | None = None,
    mcp_client: object | None = None,
) -> FastAPI:
    """Build the local app. Clients are injected so tests make no live calls."""
    config = Path(config_path)
    root = Path(runs_dir) if runs_dir is not None else config.parent / "data" / "runs"
    runner = JobRunner(
        config,
        runs_dir=root,
        model_client=model_client,
        mcp_client=mcp_client,
    )
    app = FastAPI(title="Groww weekly pulse")
    app.state.runs_root = root
    app.state.runner = runner
    app.state.config_path = config

    @app.middleware("http")
    async def _api_no_store(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_STATIC)

    @app.get("/api/weeks")
    def list_weeks() -> dict[str, object]:
        return {"weeks": _list_weeks(root)}

    @app.get("/api/weeks/{week}")
    def read_week(week: str) -> dict[str, object]:
        _check_week(week)
        return _week_payload(root, week)

    @app.get("/api/weeks/{week}/job")
    def read_job(week: str) -> dict[str, str]:
        _check_week(week)
        return runner.snapshot(week).as_dict()

    @app.post("/api/weeks/{week}/run", status_code=202)
    def start_run(week: str, request: Request) -> dict[str, str]:
        _require_write_token(request)
        _check_week(week)
        try:
            return runner.start(week).as_dict()
        except JobBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/weeks/{week}/publish")
    def publish_saved(week: str, request: Request) -> dict[str, object]:
        """Publish the saved pulse. Does not ingest, cluster, or compose."""
        _require_write_token(request)
        _check_week(week)
        if runner.running(week):
            raise HTTPException(
                status_code=409, detail=f"a run for {week} is already in progress"
            )
        week_dir = root / week
        manifest_path = week_dir / "manifest.json"
        if not manifest_path.is_file():
            raise HTTPException(status_code=404, detail=f"no run folder for {week}")
        manifest = _read_json(manifest_path)
        status = str(manifest.get("status", "")) if manifest else ""
        if status not in _PUBLISH_FROM:
            raise HTTPException(
                status_code=409,
                detail=f"run is not ready to publish: {status or 'missing'}",
            )
        publish_saved_week(config, week, client=mcp_client, runs_dir=root)
        return _week_payload(root, week)

    return app


def _require_write_token(request: Request) -> None:
    """Reject Run and Publish when ``PULSE_API_TOKEN`` is set and the bearer does not match.

    An empty variable leaves local ``serve`` open. The page does not send the
    header; Vercel adds it on the ``/api`` rewrite.
    """
    expected = os.environ.get("PULSE_API_TOKEN", "").strip()
    if not expected:
        return
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        presented.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


def _check_week(week: str) -> None:
    try:
        parse_iso_week(week)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _list_weeks(root: Path) -> list[dict[str, str]]:
    if not root.is_dir():
        return []
    found: list[dict[str, str]] = []
    for child in root.iterdir():
        if not child.is_dir() or not (child / "manifest.json").is_file():
            continue
        try:
            parse_iso_week(child.name)
        except ValueError:
            continue
        manifest = _read_json(child / "manifest.json") or {}
        found.append({"week": child.name, "status": str(manifest.get("status", ""))})
    found.sort(key=lambda item: item["week"], reverse=True)
    return found


def _week_payload(root: Path, week: str) -> dict[str, object]:
    week_dir = root / week
    kept_count, ratings = _rating_counts(week_dir / "reviews.jsonl")
    note_path = week_dir / "pulse.txt"
    note = note_path.read_text(encoding="utf-8") if note_path.is_file() else ""
    return {
        "week": week,
        "manifest": _read_json(week_dir / "manifest.json"),
        "catalog": _read_json(week_dir / "catalog.json"),
        "pulse": _read_json(week_dir / "pulse.json"),
        "note": note,
        "kept_count": kept_count,
        "ratings": ratings,
    }


def _rating_counts(path: Path) -> tuple[int, dict[str, int]]:
    counts = {star: 0 for star in _RATINGS}
    if not path.is_file():
        return 0, counts
    reviews = load_reviews_jsonl(path)
    for review in reviews:
        key = str(review.rating)
        if key in counts:
            counts[key] += 1
    return len(reviews), counts


def _read_json(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path.name} must be an object")
    return loaded
