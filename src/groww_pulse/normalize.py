"""Trim export rows, parse dates, and collapse duplicate review ids."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone

from groww_pulse.models import Review

_UNIX_SECONDS = re.compile(r"^\d{10,13}$")


def review_id(app_id: str, created_at: str, text: str) -> str:
    """Stable id from the app, the ISO date, and the trimmed review text."""
    payload = f"{app_id}|{created_at}|{text}".encode()
    return hashlib.sha256(payload).hexdigest()


def parse_rating(value: object) -> int | None:
    """Return an integer star rating from 1 to 5, or None when the value is unusable."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        rating = value
    elif isinstance(value, float):
        if not value.is_integer():
            return None
        rating = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
        if not number.is_integer():
            return None
        rating = int(number)
    else:
        return None
    if 1 <= rating <= 5:
        return rating
    return None


def parse_review_date(value: object) -> date | None:
    """Parse an export date into a calendar date. Time zones convert to UTC."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).date()
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return _from_unix(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if _UNIX_SECONDS.fullmatch(text):
        return _from_unix(int(text))
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).date()
        return parsed.date()
    for fmt in ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def normalize_rows(
    rows: list[dict[str, str]],
    *,
    window_start: date,
    window_end: date,
) -> tuple[list[Review], int]:
    """Keep in-window rows with a body and a 1–5 rating. First duplicate id wins."""
    if window_end < window_start:
        raise ValueError("window end is before window start")
    reviews: list[Review] = []
    seen: set[str] = set()
    dropped = 0
    for row in rows:
        review = _normalize_row(row, window_start=window_start, window_end=window_end)
        if review is None:
            dropped += 1
            continue
        if review.id in seen:
            dropped += 1
            continue
        seen.add(review.id)
        reviews.append(review)
    return reviews, dropped


def _normalize_row(
    row: dict[str, str],
    *,
    window_start: date,
    window_end: date,
) -> Review | None:
    text = row.get("text", "").strip()
    if not text:
        return None
    rating = parse_rating(row.get("rating", ""))
    if rating is None:
        return None
    created = parse_review_date(row.get("date", ""))
    if created is None or created < window_start or created > window_end:
        return None
    app_id = row.get("app_id", "").strip()
    if not app_id:
        return None
    title = row.get("title", "").strip()
    created_at = created.isoformat()
    return Review(
        id=review_id(app_id, created_at, text),
        store="play",
        app_id=app_id,
        rating=rating,
        title=title,
        text=text,
        created_at=created_at,
    )


def _from_unix(value: int | float) -> date | None:
    seconds = float(value)
    if seconds > 10_000_000_000:
        seconds /= 1000
    if seconds < 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None
