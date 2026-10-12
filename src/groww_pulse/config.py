"""Load config.yaml and a local .env file. Reject a window outside 8–12 weeks."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

WINDOW_WEEKS_MIN = 8
WINDOW_WEEKS_MAX = 12


@dataclass(frozen=True)
class AppSource:
    store: str
    app_id: str
    export: str


@dataclass(frozen=True)
class Config:
    product_name: str
    window_weeks: int
    apps: tuple[AppSource, ...]
    email_to: str
    doc_id: str
    doc_title_pattern: str
    max_themes: int
    pulse_theme_count: int
    quote_count: int
    action_count: int
    target_words: int
    max_words: int


def load_env_file(path: Path | str) -> None:
    """Load KEY=VALUE lines into the environment.

    Existing variables are left unchanged. Blank lines and ``#`` comments
    are ignored. A missing file does nothing.
    """
    env_path = Path(path)
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def load_config(path: Path | str) -> Config:
    """Load a config file. Raise ValueError when window_weeks is outside 8–12."""
    config_path = Path(path)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"config must be a mapping: {config_path}")

    window_weeks = raw.get("window_weeks")
    if isinstance(window_weeks, bool) or not isinstance(window_weeks, int):
        raise ValueError(
            f"window_weeks must be an integer from {WINDOW_WEEKS_MIN} to "
            f"{WINDOW_WEEKS_MAX}, got {window_weeks!r}"
        )
    if not WINDOW_WEEKS_MIN <= window_weeks <= WINDOW_WEEKS_MAX:
        raise ValueError(
            f"window_weeks must be from {WINDOW_WEEKS_MIN} to {WINDOW_WEEKS_MAX}, "
            f"got {window_weeks}"
        )

    apps_raw = raw.get("apps")
    if not isinstance(apps_raw, list) or not apps_raw:
        raise ValueError("apps must be a non-empty list")
    apps = tuple(_load_app(item) for item in apps_raw)

    return Config(
        product_name=_require_str(raw, "product_name"),
        window_weeks=window_weeks,
        apps=apps,
        email_to=_require_str(raw, "email_to"),
        doc_id=_optional_str(raw, "doc_id"),
        doc_title_pattern=_require_str(raw, "doc_title_pattern"),
        max_themes=_require_int(raw, "max_themes"),
        pulse_theme_count=_require_int(raw, "pulse_theme_count"),
        quote_count=_require_int(raw, "quote_count"),
        action_count=_require_int(raw, "action_count"),
        target_words=_require_int(raw, "target_words"),
        max_words=_require_int(raw, "max_words"),
    )


def _load_app(item: object) -> AppSource:
    if not isinstance(item, dict):
        raise ValueError("each app must be a mapping")
    return AppSource(
        store=_require_str(item, "store"),
        app_id=_require_str(item, "app_id"),
        export=_require_str(item, "export"),
    )


def _optional_str(raw: dict, key: str) -> str:
    if key not in raw:
        return ""
    value = raw.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _require_str(raw: dict, key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _require_int(raw: dict, key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value
