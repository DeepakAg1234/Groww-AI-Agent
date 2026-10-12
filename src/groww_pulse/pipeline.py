"""Run one ISO week from ingest through compose, and publish a saved pulse.

``run`` does not call MCP. ``publish`` appends the saved note and sends it.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from groww_pulse.checks import (
    ACTION_COUNT,
    LENGTH,
    PRIVACY,
    QUOTE_COUNT,
    THEME_CAP,
    THEME_MATCH,
    VERBATIM,
    WINDOW,
    check_directory,
    load_reviews_jsonl,
    write_reviews_jsonl,
)
from groww_pulse.cluster import UNCLASSIFIED, ClusterError, cluster_directory
from groww_pulse.compose import ComposeError, compose_directory
from groww_pulse.config import Config, load_config
from groww_pulse.ingest.file_export import load_reviews
from groww_pulse.model import LiveModelClient, ModelClient, ModelError
from groww_pulse.models import Review, Window
from groww_pulse.publish.mcp import LiveMcpClient, McpClient
from groww_pulse.publish.publisher import publish_saved_run
from groww_pulse.weeks import parse_iso_week, week_window

STATUS_READY = "ready"
STATUS_NO_REVIEWS = "no_reviews"
STATUS_SHORT_CATALOG = "short_catalog"
STATUS_FAILED = "failed"

_CHECK_RULES = (
    THEME_CAP,
    QUOTE_COUNT,
    VERBATIM,
    THEME_MATCH,
    ACTION_COUNT,
    LENGTH,
    PRIVACY,
    WINDOW,
)


@dataclass(frozen=True)
class CleanHandoff:
    path: Path
    rows_read: int
    rows_kept: int
    rows_dropped: int
    empty_exports: tuple[str, ...]


@dataclass(frozen=True)
class RunResult:
    status: str
    run_dir: Path
    manifest_path: Path


def write_clean_reviews(
    config_path: Path | str,
    week: str,
    *,
    runs_dir: Path | str | None = None,
) -> Path:
    """Filter the configured Play export and write ``reviews.jsonl``.

    The raw export is left unchanged. The written file is the input for
    clustering, compose, and grounding checks. ``theme_id`` is empty.
    """
    return _collect_clean_reviews(config_path, week, runs_dir=runs_dir).path


def run_week(
    config_path: Path | str,
    week: str,
    *,
    client: ModelClient | None = None,
    runs_dir: Path | str | None = None,
) -> RunResult:
    """Ingest, cluster, rank, compose, and check one week.

    The week's folder is replaced. ``reviews.jsonl`` is rewritten before
    any model call, so theme ids start empty. A window with no reviews
    stops before the model. Fewer than three non-empty themes stops
    before compose. This command does not publish.
    """
    config, week_dir = _prepare_week(config_path, week, runs_dir)
    handoff = _collect_clean_reviews(config_path, week, runs_dir=runs_dir)
    base = _base_manifest(week, config, handoff)

    if handoff.rows_kept == 0:
        names = ", ".join(handoff.empty_exports) or "none"
        return _commit(
            week_dir,
            {
                **base,
                "themes": [],
                "top_theme_ids": [],
                "unclassified_count": 0,
                "checks_ran": False,
                "check_results": [],
                "status": STATUS_NO_REVIEWS,
                "reason": f"no reviews in window: {names}",
                "doc_url": "",
                "draft_id": "",
                "message_id": "",
            },
        )

    try:
        if client is None:
            client = LiveModelClient.from_env()
        cluster_directory(
            week_dir,
            client,
            max_themes=config.max_themes,
            pulse_theme_count=config.pulse_theme_count,
        )
    except (ClusterError, ModelError) as exc:
        return _commit(week_dir, _failed_manifest(week_dir, base, config, str(exc)))

    themes, top_theme_ids, unclassified_count = _snapshot(week_dir)
    populated = sum(1 for theme in themes if int(theme["review_count"]) > 0)
    if populated < config.pulse_theme_count:
        return _commit(
            week_dir,
            {
                **base,
                "themes": themes,
                "top_theme_ids": top_theme_ids,
                "unclassified_count": unclassified_count,
                "checks_ran": False,
                "check_results": [],
                "status": STATUS_SHORT_CATALOG,
                "reason": (
                    f"fewer than {config.pulse_theme_count} themes have at least one review"
                ),
                "doc_url": "",
                "draft_id": "",
                "message_id": "",
            },
        )

    window_start, window_end = week_window(week, config.window_weeks)
    try:
        compose_directory(
            week_dir,
            client,
            week=week,
            window=Window(start=window_start.isoformat(), end=window_end.isoformat()),
            product_name=config.product_name,
            max_words=config.max_words,
            max_themes=config.max_themes,
            pulse_theme_count=config.pulse_theme_count,
            quote_count=config.quote_count,
            action_count=config.action_count,
        )
    except (ComposeError, ModelError) as exc:
        return _commit(week_dir, _failed_manifest(week_dir, base, config, str(exc)))

    check_results = _check_results(week_dir, config)
    passed = bool(check_results) and all(item["passed"] for item in check_results)
    pulse_path = week_dir / "pulse.json"
    pulse = json.loads(pulse_path.read_text(encoding="utf-8")) if pulse_path.is_file() else {}
    return _commit(
        week_dir,
        {
            **base,
            "themes": themes,
            "top_theme_ids": top_theme_ids,
            "unclassified_count": unclassified_count,
            "checks_ran": True,
            "check_results": check_results,
            "status": STATUS_READY if passed else STATUS_FAILED,
            "reason": "" if passed else "grounding checks failed",
            "doc_url": str(pulse.get("doc_url", "")),
            "draft_id": str(pulse.get("draft_id", "")),
            "message_id": str(pulse.get("message_id", "")),
        },
    )


def publish_week(
    config_path: Path | str,
    week: str,
    *,
    client: McpClient | None = None,
    runs_dir: Path | str | None = None,
) -> RunResult:
    """Append and send a saved pulse. Does not ingest, cluster, or compose."""
    config, week_dir = _resolve(config_path, week, runs_dir)
    if not week_dir.is_dir():
        raise FileNotFoundError(f"no run folder for {week}: {week_dir}")
    manifest = _read_manifest(week_dir)
    if client is None:
        client = LiveMcpClient.from_env()
    update = publish_saved_run(
        config,
        week,
        week_dir,
        client,
        manifest=manifest,
    )
    manifest.update(
        {
            "status": update.status,
            "reason": update.reason,
            "doc_url": update.doc_url,
            "message_id": update.message_id,
            "authorization_url": update.authorization_url,
        }
    )
    return _commit(week_dir, manifest)


def _collect_clean_reviews(
    config_path: Path | str,
    week: str,
    *,
    runs_dir: Path | str | None = None,
) -> CleanHandoff:
    parse_iso_week(week)
    config_file = Path(config_path)
    config = load_config(config_file)
    root = config_file.parent
    window_start, window_end = week_window(week, config.window_weeks)
    reviews: list[Review] = []
    rows_read = 0
    rows_kept = 0
    rows_dropped = 0
    empty: list[str] = []
    for app in config.apps:
        if app.store != "play":
            continue
        export = Path(app.export)
        if not export.is_absolute():
            export = root / export
        result = load_reviews(
            export,
            app_id=app.app_id,
            window_start=window_start,
            window_end=window_end,
        )
        reviews.extend(result.reviews)
        rows_read += result.rows_read
        rows_kept += result.rows_kept
        rows_dropped += result.rows_dropped
        if result.rows_kept == 0:
            empty.append(app.export)
    output_root = Path(runs_dir) if runs_dir is not None else root / "data" / "runs"
    destination = output_root / week / "reviews.jsonl"
    write_reviews_jsonl(destination, reviews)
    return CleanHandoff(
        path=destination,
        rows_read=rows_read,
        rows_kept=rows_kept,
        rows_dropped=rows_dropped,
        empty_exports=tuple(empty),
    )


def _prepare_week(
    config_path: Path | str,
    week: str,
    runs_dir: Path | str | None,
) -> tuple[Config, Path]:
    """Drop the previous week folder so this run replaces it."""
    config, week_dir = _resolve(config_path, week, runs_dir)
    if week_dir.exists():
        shutil.rmtree(week_dir)
    return config, week_dir


def _resolve(
    config_path: Path | str,
    week: str,
    runs_dir: Path | str | None,
) -> tuple[Config, Path]:
    parse_iso_week(week)
    config_file = Path(config_path)
    config = load_config(config_file)
    root = config_file.parent
    output_root = Path(runs_dir) if runs_dir is not None else root / "data" / "runs"
    return config, output_root / week


def _read_manifest(week_dir: Path) -> dict[str, object]:
    path = week_dir / "manifest.json"
    if not path.is_file():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"manifest must be an object: {path}")
    return loaded


def _base_manifest(week: str, config: Config, handoff: CleanHandoff) -> dict[str, object]:
    start, end = week_window(week, config.window_weeks)
    return {
        "week": week,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "rows_read": handoff.rows_read,
        "rows_kept": handoff.rows_kept,
        "rows_dropped": handoff.rows_dropped,
        "empty_exports": list(handoff.empty_exports),
    }


def _failed_manifest(
    week_dir: Path, base: dict[str, object], config: Config, reason: str
) -> dict[str, object]:
    themes, top_theme_ids, unclassified_count = _snapshot(week_dir)
    check_results = _check_results(week_dir, config)
    pulse_path = week_dir / "pulse.json"
    pulse: dict[str, object] = {}
    if pulse_path.is_file():
        loaded = json.loads(pulse_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            pulse = loaded
    return {
        **base,
        "themes": themes,
        "top_theme_ids": top_theme_ids,
        "unclassified_count": unclassified_count,
        "checks_ran": check_results is not None,
        "check_results": check_results or [],
        "status": STATUS_FAILED,
        "reason": reason,
        "doc_url": str(pulse.get("doc_url", "")),
        "draft_id": str(pulse.get("draft_id", "")),
        "message_id": str(pulse.get("message_id", "")),
    }


def _snapshot(week_dir: Path) -> tuple[list[dict[str, object]], list[str], int]:
    """Theme counts, top ids, and the unclassified count from the run folder."""
    reviews_path = week_dir / "reviews.jsonl"
    reviews = load_reviews_jsonl(reviews_path) if reviews_path.is_file() else []
    unclassified_count = sum(1 for review in reviews if review.theme_id == UNCLASSIFIED)
    catalog_path = week_dir / "catalog.json"
    if not catalog_path.is_file():
        return [], [], unclassified_count
    raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return [], [], unclassified_count
    counts: dict[str, int] = {}
    for review in reviews:
        if review.theme_id and review.theme_id != UNCLASSIFIED:
            counts[review.theme_id] = counts.get(review.theme_id, 0) + 1
    themes: list[dict[str, object]] = []
    items = raw.get("themes")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            theme_id = str(item.get("id", "")).strip()
            if not theme_id:
                continue
            themes.append(
                {
                    "id": theme_id,
                    "name": str(item.get("name", "")).strip(),
                    "review_count": counts.get(theme_id, 0),
                }
            )
    top_raw = raw.get("top_theme_ids")
    top = [str(theme_id) for theme_id in top_raw] if isinstance(top_raw, list) else []
    return themes, top, unclassified_count


def _check_results(week_dir: Path, config: Config) -> list[dict[str, object]] | None:
    if not (week_dir / "pulse.json").is_file() or not (week_dir / "pulse.txt").is_file():
        return None
    if not (week_dir / "reviews.jsonl").is_file():
        return None
    failures = {
        failure.rule: failure.detail
        for failure in check_directory(
            week_dir,
            max_themes=config.max_themes,
            pulse_theme_count=config.pulse_theme_count,
            quote_count=config.quote_count,
            action_count=config.action_count,
            max_words=config.max_words,
        )
    }
    return [
        {
            "rule": rule,
            "passed": rule not in failures,
            "detail": failures.get(rule, ""),
        }
        for rule in _CHECK_RULES
    ]


def _commit(week_dir: Path, manifest: dict[str, object]) -> RunResult:
    week_dir.mkdir(parents=True, exist_ok=True)
    destination = week_dir / "manifest.json"
    destination.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return RunResult(
        status=str(manifest["status"]),
        run_dir=week_dir,
        manifest_path=destination,
    )
