"""Accept or reject a pulse before anything is published.

Checks read ``reviews.jsonl``, ``pulse.json``, and the rendered note in
``pulse.txt``. They do not call a model or an MCP server.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from groww_pulse.models import Action, Pulse, Quote, Review, Theme, Window
from groww_pulse.privacy import contains_pii, personal_names

THEME_CAP = "theme_cap"
QUOTE_COUNT = "quote_count"
VERBATIM = "verbatim"
THEME_MATCH = "theme_match"
ACTION_COUNT = "action_count"
LENGTH = "length"
PRIVACY = "privacy"
WINDOW = "window"


@dataclass(frozen=True)
class CheckFailure:
    rule: str
    detail: str


def check_directory(
    path: Path | str,
    *,
    max_themes: int = 5,
    pulse_theme_count: int = 3,
    quote_count: int = 3,
    action_count: int = 3,
    max_words: int = 280,
) -> tuple[CheckFailure, ...]:
    """Run every grounding check on one run folder."""
    run = Path(path)
    reviews = load_reviews_jsonl(run / "reviews.jsonl")
    pulse = load_pulse(run / "pulse.json")
    note = (run / "pulse.txt").read_text(encoding="utf-8")
    return check_pulse(
        reviews,
        pulse,
        note,
        max_themes=max_themes,
        pulse_theme_count=pulse_theme_count,
        quote_count=quote_count,
        action_count=action_count,
        max_words=max_words,
    )


def check_pulse(
    reviews: list[Review],
    pulse: Pulse,
    note: str,
    *,
    max_themes: int = 5,
    pulse_theme_count: int = 3,
    quote_count: int = 3,
    action_count: int = 3,
    max_words: int = 280,
) -> tuple[CheckFailure, ...]:
    """Return one failure per broken rule, in a stable order."""
    by_id = {review.id: review for review in reviews}
    checks = (
        _theme_cap(pulse, note, max_themes, pulse_theme_count),
        _quote_count(pulse, quote_count),
        _verbatim(pulse, by_id),
        _theme_match(pulse, by_id),
        _action_count(pulse, action_count),
        _length(note, max_words),
        _privacy(note, pulse.quotes),
        _window(pulse, by_id),
    )
    return tuple(failure for failure in checks if failure is not None)


def write_reviews_jsonl(path: Path | str, reviews: list[Review]) -> None:
    """Write one normalized review per line. Later stages read this file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(asdict(review)) + "\n" for review in reviews)
    destination.write_text(payload, encoding="utf-8")


def load_reviews_jsonl(path: Path | str) -> list[Review]:
    """Load normalized reviews. Blank lines are ignored."""
    reviews: list[Review] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        if not isinstance(raw, dict):
            raise ValueError("each reviews.jsonl line must be an object")
        reviews.append(
            Review(
                id=_require_str(raw, "id"),
                store=_require_str(raw, "store"),
                app_id=_require_str(raw, "app_id"),
                rating=_require_int(raw, "rating"),
                title=str(raw.get("title", "")),
                text=_require_str(raw, "text"),
                created_at=_require_str(raw, "created_at"),
                theme_id=str(raw.get("theme_id", "")),
            )
        )
    return reviews


def load_pulse(path: Path | str) -> Pulse:
    """Load a structured pulse from pulse.json."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("pulse.json must be an object")
    window = raw.get("window")
    if not isinstance(window, dict):
        raise ValueError("pulse.window must be an object")
    return Pulse(
        week=_require_str(raw, "week"),
        window=Window(
            start=_require_str(window, "start"),
            end=_require_str(window, "end"),
        ),
        review_count=_require_int(raw, "review_count"),
        themes=tuple(_load_theme(item) for item in _require_list(raw, "themes")),
        top_theme_ids=tuple(
            str(item) for item in _require_list(raw, "top_theme_ids")
        ),
        quotes=tuple(_load_quote(item) for item in _require_list(raw, "quotes")),
        actions=tuple(_load_action(item) for item in _require_list(raw, "actions")),
        word_count=int(raw.get("word_count", 0)),
        doc_url=str(raw.get("doc_url", "")),
        draft_id=str(raw.get("draft_id", "")),
    )


def _theme_cap(
    pulse: Pulse,
    note: str,
    max_themes: int,
    pulse_theme_count: int,
) -> CheckFailure | None:
    problems: list[str] = []
    catalog_size = len(pulse.themes)
    if not 1 <= catalog_size <= max_themes:
        problems.append(f"catalog has {catalog_size} themes")
    catalog_ids = {theme.id for theme in pulse.themes}
    top_ids = pulse.top_theme_ids
    if len(top_ids) != pulse_theme_count or len(set(top_ids)) != len(top_ids):
        problems.append(f"note highlights {len(top_ids)} themes")
    missing = [theme_id for theme_id in top_ids if theme_id not in catalog_ids]
    if missing:
        problems.append("highlighted theme is missing from the catalog")
    top = set(top_ids)
    extras = [
        theme.name
        for theme in pulse.themes
        if theme.id not in top and _mentions(note, theme.name)
    ]
    if extras:
        problems.append("note names " + ", ".join(extras))
    if not problems:
        return None
    return CheckFailure(THEME_CAP, "; ".join(problems))


def _quote_count(pulse: Pulse, quote_count: int) -> CheckFailure | None:
    found = len(pulse.quotes)
    if found == quote_count:
        return None
    return CheckFailure(QUOTE_COUNT, f"found {found} quotes")


def _verbatim(
    pulse: Pulse, by_id: dict[str, Review]
) -> CheckFailure | None:
    problems: list[str] = []
    for quote in pulse.quotes:
        review = by_id.get(quote.review_id)
        if review is None:
            problems.append(f"missing review {quote.review_id}")
            continue
        if not _is_span(quote.text, review.text):
            problems.append(f"quote is not a span of {quote.review_id}")
    if not problems:
        return None
    return CheckFailure(VERBATIM, "; ".join(problems))


def _theme_match(
    pulse: Pulse, by_id: dict[str, Review]
) -> CheckFailure | None:
    top = set(pulse.top_theme_ids)
    problems: list[str] = []
    for quote in pulse.quotes:
        review = by_id.get(quote.review_id)
        if review is None:
            problems.append(f"missing review {quote.review_id}")
            continue
        if review.theme_id not in top:
            problems.append(
                f"{quote.review_id} theme is {review.theme_id or 'empty'}"
            )
    if not problems:
        return None
    return CheckFailure(THEME_MATCH, "; ".join(problems))


def _action_count(pulse: Pulse, action_count: int) -> CheckFailure | None:
    problems: list[str] = []
    if len(pulse.actions) != action_count:
        problems.append(f"found {len(pulse.actions)} actions")
    names = {theme.id: theme.name for theme in pulse.themes}
    top = set(pulse.top_theme_ids)
    for action in pulse.actions:
        if action.theme_id not in top:
            problems.append(f"action theme {action.theme_id} is not highlighted")
            continue
        name = names.get(action.theme_id, "")
        if not name or not _mentions(action.text, name):
            problems.append(f"action does not name {name or action.theme_id}")
    if not problems:
        return None
    return CheckFailure(ACTION_COUNT, "; ".join(problems))


def _length(note: str, max_words: int) -> CheckFailure | None:
    count = len(note.split())
    if count <= max_words:
        return None
    return CheckFailure(LENGTH, f"note has {count} words")


def _privacy(note: str, quotes: tuple[Quote, ...]) -> CheckFailure | None:
    problems: list[str] = []
    if contains_pii(note):
        problems.append("note contains an email, phone, device id, or handle")
    if "[redacted]" in note.casefold():
        problems.append("note contains [redacted]")
    names: list[str] = []
    for quote in quotes:
        names.extend(personal_names(quote.text))
    if names:
        problems.append("quote contains a personal name: " + ", ".join(names))
    if not problems:
        return None
    return CheckFailure(PRIVACY, "; ".join(problems))


def _window(pulse: Pulse, by_id: dict[str, Review]) -> CheckFailure | None:
    start = pulse.window.start
    end = pulse.window.end
    problems: list[str] = []
    for quote in pulse.quotes:
        review = by_id.get(quote.review_id)
        if review is None:
            problems.append(f"missing review {quote.review_id}")
            continue
        if review.created_at < start or review.created_at > end:
            problems.append(
                f"{quote.review_id} dated {review.created_at} is outside the window"
            )
    if not problems:
        return None
    return CheckFailure(WINDOW, "; ".join(problems))


def _is_span(quote: str, review_text: str) -> bool:
    """True when the quote is a contiguous span after whitespace normalization."""
    normalized_quote = " ".join(quote.split())
    normalized_review = " ".join(review_text.split())
    return bool(normalized_quote) and normalized_quote in normalized_review


def _mentions(text: str, name: str) -> bool:
    if not name.strip():
        return False
    pattern = r"(?<!\w)" + re.escape(name) + r"(?!\w)"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _load_theme(item: object) -> Theme:
    raw = _as_dict(item, "theme")
    return Theme(
        id=_require_str(raw, "id"),
        name=_require_str(raw, "name"),
        definition=str(raw.get("definition", "")),
        review_count=int(raw.get("review_count", 0)),
        average_rating=float(raw.get("average_rating", 0)),
        summary=str(raw.get("summary", "")),
    )


def _load_quote(item: object) -> Quote:
    raw = _as_dict(item, "quote")
    return Quote(
        text=_require_str(raw, "text"),
        rating=_require_int(raw, "rating"),
        store=str(raw.get("store", "play")),
        theme_id=_require_str(raw, "theme_id"),
        review_id=_require_str(raw, "review_id"),
    )


def _load_action(item: object) -> Action:
    raw = _as_dict(item, "action")
    return Action(theme_id=_require_str(raw, "theme_id"), text=_require_str(raw, "text"))


def _as_dict(item: object, label: str) -> dict:
    if not isinstance(item, dict):
        raise ValueError(f"each {label} must be an object")
    return item


def _require_list(raw: dict, key: str) -> list:
    value = raw.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return value


def _require_str(raw: dict, key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _require_int(raw: dict, key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value
