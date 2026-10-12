"""Read a Play Store CSV or JSON export into anonymous reviews."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from groww_pulse.models import Review
from groww_pulse.normalize import normalize_rows
from groww_pulse.privacy import apply_privacy

_ALIASES: dict[str, tuple[str, ...]] = {
    "rating": ("rating", "score", "stars"),
    "text": ("text", "content", "review", "body"),
    "date": ("date", "created_at", "at", "updated"),
    "title": ("title", "summary"),
    "app_id": ("app_id", "package"),
}
_REQUIRED = ("rating", "text", "date")


@dataclass(frozen=True)
class IngestResult:
    reviews: tuple[Review, ...]
    rows_read: int
    rows_kept: int
    rows_dropped: int


def load_reviews(
    path: Path | str,
    *,
    app_id: str,
    window_start: date,
    window_end: date,
) -> IngestResult:
    """Ingest one export: normalize, dedupe, redact, and count drops."""
    rows = read_rows(path, default_app_id=app_id)
    normalized, _ = normalize_rows(
        rows, window_start=window_start, window_end=window_end
    )
    private, _ = apply_privacy(normalized)
    kept = tuple(private)
    read = len(rows)
    return IngestResult(
        reviews=kept,
        rows_read=read,
        rows_kept=len(kept),
        rows_dropped=read - len(kept),
    )


def read_rows(path: Path | str, *, default_app_id: str) -> list[dict[str, str]]:
    """Return canonical rows. Reviewer identity columns are not copied."""
    export = Path(path)
    suffix = export.suffix.lower()
    if suffix == ".csv":
        records = _read_csv(export)
    elif suffix == ".json":
        records = _read_json(export)
    else:
        raise ValueError(f"unsupported export type: {export.suffix or export.name}")
    rows: list[dict[str, str]] = []
    for record in records:
        mapped = _canonicalize(record)
        if not mapped.get("app_id", "").strip():
            mapped["app_id"] = default_app_id
        rows.append(mapped)
    return rows


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"export has no header: {path}")
        _header_map(reader.fieldnames)
        return [_row_from_record(raw) for raw in reader]


def _read_json(path: Path) -> list[dict[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    records = _json_records(payload)
    rows: list[dict[str, str]] = []
    keys: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("each JSON review must be an object")
        keys.extend(key for key in record if isinstance(key, str))
        rows.append(_row_from_record(record))
    if rows:
        _header_map(keys)
    return rows


def _json_records(payload: object) -> list[object]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("reviews", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise ValueError("JSON export must be a list of reviews or an object with reviews")


def _header_map(fieldnames: object) -> dict[str, str]:
    if not isinstance(fieldnames, (list, tuple)) and not hasattr(fieldnames, "__iter__"):
        raise ValueError("export header is missing")
    lookup: dict[str, str] = {}
    for name in fieldnames:
        if not isinstance(name, str) or not name.strip():
            continue
        lookup.setdefault(_normal_header(name), name)
    mapping: dict[str, str] = {}
    for canonical, aliases in _ALIASES.items():
        for alias in aliases:
            if alias in lookup:
                mapping[canonical] = lookup[alias]
                break
    missing = [name for name in _REQUIRED if name not in mapping]
    if missing:
        raise ValueError(
            "export missing required columns: " + ", ".join(missing)
        )
    return mapping


def _row_from_record(record: dict[object, object]) -> dict[str, str]:
    lookup: dict[str, object] = {}
    for key in record:
        if isinstance(key, str) and key.strip():
            lookup.setdefault(_normal_header(key), key)
    row = {name: "" for name in _ALIASES}
    for canonical, aliases in _ALIASES.items():
        for alias in aliases:
            source = lookup.get(alias)
            if source is None:
                continue
            value = record.get(source)
            row[canonical] = "" if value is None else str(value)
            break
    return row


def _canonicalize(row: dict[str, str]) -> dict[str, str]:
    canonical = {name: "" for name in _ALIASES}
    canonical.update(row)
    return canonical


def _normal_header(name: str) -> str:
    return "_".join(name.strip().lower().replace("-", " ").split())
