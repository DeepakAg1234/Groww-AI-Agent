"""ISO week parsing and the review window for a run."""

from __future__ import annotations

import re
from datetime import date, timedelta

_ISO_WEEK = re.compile(r"^(\d{4})-W(\d{2})$")
WINDOW_WEEKS_MIN = 8
WINDOW_WEEKS_MAX = 12


def parse_iso_week(value: str) -> date:
    """Return the Monday of an ISO week, or raise ValueError."""
    match = _ISO_WEEK.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid ISO week: {value!r}; expected YYYY-Www")
    year = int(match.group(1))
    week = int(match.group(2))
    try:
        return date.fromisocalendar(year, week, 1)
    except ValueError as exc:
        raise ValueError(f"invalid ISO week: {value!r}") from exc


def week_window(iso_week: str, window_weeks: int) -> tuple[date, date]:
    """Return the Monday-to-Sunday span of `window_weeks` ISO weeks.

    The span ends on the Sunday of `iso_week`. For 2026-W41 and 10 weeks
    that is 2026-08-03 through 2026-10-11.
    """
    if isinstance(window_weeks, bool) or not isinstance(window_weeks, int):
        raise ValueError(f"window_weeks must be an integer, got {window_weeks!r}")
    if not WINDOW_WEEKS_MIN <= window_weeks <= WINDOW_WEEKS_MAX:
        raise ValueError(
            f"window_weeks must be from {WINDOW_WEEKS_MIN} to {WINDOW_WEEKS_MAX}, "
            f"got {window_weeks}"
        )
    monday = parse_iso_week(iso_week)
    start = monday - timedelta(weeks=window_weeks - 1)
    end = monday + timedelta(days=6)
    return start, end
