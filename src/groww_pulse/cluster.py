"""Freeze a theme catalog, assign every review, and rank the top three.

Reads ``reviews.jsonl`` only. The raw Play export is not opened again.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from groww_pulse.checks import load_reviews_jsonl, write_reviews_jsonl
from groww_pulse.model import ModelClient
from groww_pulse.models import Review, Theme

MAX_THEMES = 5
PULSE_THEME_COUNT = 3
BATCH_SIZE = 30
SAMPLE_TARGETS = ((1, 1, 24), (2, 3, 12), (4, 5, 24))
UNCLASSIFIED = "unclassified"
SEED_HINTS = (
    "orders and trading",
    "charts, speed, and stability",
    "customer support",
    "charges and brokerage",
    "payments, withdrawals, and mutual funds",
)
_THEME_ID = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


class ClusterError(RuntimeError):
    """Catalog or assignment could not be accepted."""


@dataclass(frozen=True)
class CatalogTheme:
    id: str
    name: str
    definition: str
    examples: tuple[str, str]


@dataclass(frozen=True)
class ClusterResult:
    themes: tuple[Theme, ...]
    top_theme_ids: tuple[str, ...]
    reviews: tuple[Review, ...]


def cluster_directory(
    run_dir: Path | str,
    client: ModelClient,
    *,
    max_themes: int = MAX_THEMES,
    pulse_theme_count: int = PULSE_THEME_COUNT,
) -> ClusterResult:
    """Cluster one run folder and write ``catalog.json`` plus theme ids.

    The catalog file is written before any assignment call. Theme ids are
    written back to ``reviews.jsonl`` only after every review has a label.
    """
    run = Path(run_dir)
    reviews_path = run / "reviews.jsonl"
    reviews = load_reviews_jsonl(reviews_path)
    if not reviews:
        raise ClusterError(f"no reviews in {reviews_path}")

    sample = sample_reviews(reviews)
    catalog = propose_catalog(client, sample, max_themes=max_themes)
    catalog_path = run / "catalog.json"
    write_catalog(catalog_path, catalog)
    labels = assign_reviews(client, reviews, catalog)
    ranked = rank_themes(catalog, reviews, labels)
    top_theme_ids = tuple(
        theme.id for theme in ranked if theme.review_count > 0
    )[:pulse_theme_count]
    labeled = [
        replace(review, theme_id=labels[review.id]) for review in reviews
    ]
    write_reviews_jsonl(reviews_path, labeled)
    write_catalog(
        catalog_path,
        catalog,
        themes=ranked,
        top_theme_ids=top_theme_ids,
    )
    return ClusterResult(
        themes=ranked,
        top_theme_ids=top_theme_ids,
        reviews=tuple(labeled),
    )


def sample_reviews(reviews: list[Review] | tuple[Review, ...]) -> list[Review]:
    """Take up to 60 reviews: 24 ones, 12 rated 2–3, 24 rated 4–5.

    Each band is round-robin across ISO weeks so one busy week does not
    fill the catalog prompt. The same input returns the same sample.
    """
    chosen: list[Review] = []
    used: set[str] = set()
    for low, high, target in SAMPLE_TARGETS:
        band = [
            review
            for review in reviews
            if low <= review.rating <= high and review.id not in used
        ]
        picked = _spread_across_weeks(band, target)
        chosen.extend(picked)
        used.update(review.id for review in picked)
    return chosen


def propose_catalog(
    client: ModelClient,
    sample: list[Review],
    *,
    max_themes: int = MAX_THEMES,
) -> tuple[CatalogTheme, ...]:
    """Ask for a catalog once, then retry once. A second failure stops the run."""
    prompt = _catalog_prompt(sample, max_themes)
    last_error = "catalog response was empty"
    for _attempt in range(2):
        raw = client.complete(prompt)
        try:
            return _parse_catalog(raw, sample, max_themes=max_themes)
        except ValueError as exc:
            last_error = str(exc)
            prompt = _catalog_prompt(sample, max_themes, rejection=last_error)
    raise ClusterError(f"catalog failed after one retry: {last_error}")


def assign_reviews(
    client: ModelClient,
    reviews: list[Review] | tuple[Review, ...],
    catalog: tuple[CatalogTheme, ...],
) -> dict[str, str]:
    """Assign every review in batches of 30. Retry a bad label once."""
    allowed = {theme.id for theme in catalog}
    labels: dict[str, str] = {}
    ordered = sorted(reviews, key=lambda review: review.id)
    for start in range(0, len(ordered), BATCH_SIZE):
        batch = ordered[start : start + BATCH_SIZE]
        labels.update(_assign_batch(client, batch, catalog, allowed))
    missing = [review.id for review in reviews if review.id not in labels]
    if missing:
        raise ClusterError(
            "assignment did not label every review: " + ", ".join(missing)
        )
    return labels


def rank_themes(
    catalog: tuple[CatalogTheme, ...],
    reviews: list[Review] | tuple[Review, ...],
    labels: dict[str, str],
) -> tuple[Theme, ...]:
    """Order themes by count, then sharper pain, then a more recent median date.

    ``unclassified`` is stored on reviews and does not enter this order.
    """
    by_id = {review.id: review for review in reviews}
    grouped: dict[str, list[Review]] = {theme.id: [] for theme in catalog}
    for review_id, theme_id in labels.items():
        if theme_id == UNCLASSIFIED:
            continue
        review = by_id.get(review_id)
        if review is not None and theme_id in grouped:
            grouped[theme_id].append(review)

    ranked: list[Theme] = []
    for theme in catalog:
        members = grouped[theme.id]
        ratings = [review.rating for review in members]
        average = sum(ratings) / len(ratings) if ratings else 0.0
        ranked.append(
            Theme(
                id=theme.id,
                name=theme.name,
                definition=theme.definition,
                review_count=len(members),
                average_rating=average,
            )
        )

    def sort_key(theme: Theme) -> tuple[int, float, int, str]:
        members = grouped[theme.id]
        if members:
            median = _median_date(review.created_at for review in members)
            median_ordinal = median.toordinal()
        else:
            median_ordinal = 0
        return (-theme.review_count, theme.average_rating, -median_ordinal, theme.id)

    ranked.sort(key=sort_key)
    return tuple(ranked)


def write_catalog(
    path: Path | str,
    catalog: tuple[CatalogTheme, ...],
    *,
    themes: tuple[Theme, ...] | None = None,
    top_theme_ids: tuple[str, ...] | None = None,
) -> None:
    """Write the frozen catalog. Counts and top ids are added after ranking."""
    counts = {theme.id: theme for theme in themes} if themes is not None else {}
    payload_themes: list[dict[str, object]] = []
    for theme in catalog:
        item: dict[str, object] = {
            "id": theme.id,
            "name": theme.name,
            "definition": theme.definition,
            "examples": list(theme.examples),
        }
        ranked = counts.get(theme.id)
        if ranked is not None:
            item["review_count"] = ranked.review_count
            item["average_rating"] = ranked.average_rating
        payload_themes.append(item)
    body: dict[str, object] = {"themes": payload_themes}
    if top_theme_ids is not None:
        body["top_theme_ids"] = list(top_theme_ids)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(body, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _spread_across_weeks(band: list[Review], target: int) -> list[Review]:
    if target <= 0 or not band:
        return []
    by_week: dict[str, list[Review]] = defaultdict(list)
    for review in band:
        by_week[_iso_week(review.created_at)].append(review)
    for week_reviews in by_week.values():
        week_reviews.sort(key=lambda review: review.id)
    weeks = sorted(by_week)
    if len(band) <= target:
        return [
            review
            for week in weeks
            for review in by_week[week]
        ]
    picked: list[Review] = []
    index = dict.fromkeys(weeks, 0)
    while len(picked) < target:
        progressed = False
        for week in weeks:
            cursor = index[week]
            bucket = by_week[week]
            if cursor >= len(bucket):
                continue
            picked.append(bucket[cursor])
            index[week] = cursor + 1
            progressed = True
            if len(picked) == target:
                break
        if not progressed:
            break
    return picked


def _assign_batch(
    client: ModelClient,
    batch: list[Review],
    catalog: tuple[CatalogTheme, ...],
    allowed: set[str],
) -> dict[str, str]:
    pending = list(batch)
    labels: dict[str, str] = {}
    rejection = ""
    for _attempt in range(2):
        if not pending:
            break
        raw = client.complete(_assign_prompt(pending, catalog, rejection=rejection))
        try:
            parsed = _parse_assignments(raw)
        except ValueError as exc:
            parsed = {}
            rejection = str(exc)
        else:
            rejection = ""
        still_pending: list[Review] = []
        reasons: list[str] = []
        for review in pending:
            label = parsed.get(review.id)
            if label is None:
                reasons.append(f"{review.id} was missing")
                still_pending.append(review)
                continue
            canonical = _canonical_label(label, allowed)
            if canonical is None:
                reasons.append(f"{review.id} labeled {label!r} is outside the catalog")
                still_pending.append(review)
                continue
            labels[review.id] = canonical
        pending = still_pending
        if pending:
            rejection = "; ".join(reasons) if reasons else rejection
    if pending:
        ids = ", ".join(review.id for review in pending)
        detail = f": {rejection}" if rejection else ""
        raise ClusterError(f"assignment failed after one retry for {ids}{detail}")
    return labels


def _catalog_prompt(
    sample: list[Review],
    max_themes: int,
    *,
    rejection: str = "",
) -> str:
    seeds = "\n".join(f"{index}. {hint}" for index, hint in enumerate(SEED_HINTS, start=1))
    retry = ""
    if rejection:
        retry = (
            "\nThe previous catalog was rejected: "
            f"{rejection}\nReturn a corrected catalog of at most {max_themes} themes.\n"
        )
    return (
        "TASK: catalog\n"
        "Group these Google Play reviews of Groww into product themes.\n"
        f"Return at most {max_themes} themes.\n\n"
        "Seed hints. Rename, merge, or drop a hint the reviews do not support:\n"
        f"{seeds}\n\n"
        "Fold smaller topics in this file (KYC, statements, IPO, login) into the "
        "closest theme. Undifferentiated praise such as \"nice app\", \"best app\", "
        "or \"easy to use\" with no product detail is not a theme. A keyboard-mash "
        "body is not a theme.\n"
        "Each theme needs an id (lowercase slug using underscores, not hyphens), "
        "a name, a one-sentence definition, and two example phrases copied from "
        "the review texts below. Copy each phrase as a contiguous span, including "
        "the review's spelling. Do not correct typos or insert an ellipsis.\n"
        f"{retry}\n"
        "Respond with JSON only:\n"
        '{"themes": [{"id": "...", "name": "...", "definition": "...", '
        '"examples": ["...", "..."]}]}\n\n'
        "Reviews:\n"
        f"{_review_lines(sample)}\n"
    )


def _assign_prompt(
    batch: list[Review],
    catalog: tuple[CatalogTheme, ...],
    *,
    rejection: str = "",
) -> str:
    catalog_payload = [
        {
            "id": theme.id,
            "name": theme.name,
            "definition": theme.definition,
            "examples": list(theme.examples),
        }
        for theme in catalog
    ]
    retry = ""
    if rejection:
        retry = (
            "\nThe previous assignment was rejected: "
            f"{rejection}\nLabel only the reviews below. "
            "Use a catalog id or \"unclassified\".\n"
        )
    return (
        "TASK: assign\n"
        "Assign each review to exactly one theme id from the catalog, or "
        f"\"{UNCLASSIFIED}\".\n"
        "Use unclassified for undifferentiated praise, keyboard mash, and reviews "
        "that match none of the themes.\n"
        "When a review matches two themes, choose the theme it spends more of its "
        "text on. Counts are exclusive. Do not invent theme ids. Echo every review id.\n"
        f"{retry}\n"
        "Catalog:\n"
        f"{json.dumps(catalog_payload, ensure_ascii=False)}\n\n"
        "Respond with JSON only:\n"
        '{"assignments": [{"id": "...", "theme_id": "..."}]}\n\n'
        "Reviews:\n"
        f"{_review_lines(batch)}\n"
    )


def _parse_catalog(
    raw: str,
    sample: list[Review],
    *,
    max_themes: int,
) -> tuple[CatalogTheme, ...]:
    payload = _extract_json(raw)
    if not isinstance(payload, dict):
        raise ValueError("catalog response must be a JSON object")
    items = payload.get("themes")
    if not isinstance(items, list) or not items:
        raise ValueError("catalog response did not include any themes")
    if len(items) > max_themes:
        raise ValueError(
            f"catalog has {len(items)} themes; the maximum is {max_themes}"
        )
    sample_texts = [_squash(review.text) for review in sample]
    themes: list[CatalogTheme] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each theme must be a JSON object")
        theme_id = _normalize_theme_id(str(item.get("id", "")))
        name = str(item.get("name", "")).strip()
        definition = str(item.get("definition", "")).strip()
        examples = item.get("examples")
        if not _THEME_ID.fullmatch(theme_id) or theme_id == UNCLASSIFIED:
            raise ValueError(f"invalid theme id {theme_id!r}")
        if theme_id in seen:
            raise ValueError(f"duplicate theme id {theme_id!r}")
        if not name or not definition:
            raise ValueError(f"theme {theme_id} needs a name and a definition")
        if (
            not isinstance(examples, list)
            or len(examples) != 2
            or not all(isinstance(phrase, str) and phrase.strip() for phrase in examples)
        ):
            raise ValueError(f"theme {theme_id} needs two example phrases")
        grounded: list[str] = []
        for original in examples:
            phrase = _ground_example(str(original), sample_texts)
            if phrase is None:
                raise ValueError(
                    f"example phrase {str(original).strip()!r} does not appear in the sample"
                )
            grounded.append(phrase)
        phrases = (grounded[0], grounded[1])
        seen.add(theme_id)
        themes.append(
            CatalogTheme(
                id=theme_id,
                name=name,
                definition=definition,
                examples=phrases,
            )
        )
    return tuple(themes)


_ASSIGNMENT_OBJECT = re.compile(
    r'\{\s*"id"\s*:\s*"([^"]+)"\s*,\s*"theme_id"\s*:\s*"([^"]+)"\s*\}'
)


def _parse_assignments(raw: str) -> dict[str, str]:
    try:
        payload = _extract_json(raw)
    except ValueError:
        payload = {"assignments": [
            {"id": review_id, "theme_id": theme_id}
            for review_id, theme_id in _ASSIGNMENT_OBJECT.findall(raw)
        ]}
    if not isinstance(payload, dict):
        raise ValueError("assignment response must be a JSON object")
    items = payload.get("assignments")
    if not isinstance(items, list) or not items:
        raise ValueError("assignment response did not include assignments")
    labels: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each assignment must be a JSON object")
        review_id = str(item.get("id", "")).strip()
        theme_id = str(item.get("theme_id", "")).strip()
        if not review_id or not theme_id:
            raise ValueError("each assignment needs an id and a theme_id")
        if review_id in labels:
            raise ValueError(f"duplicate assignment for {review_id}")
        labels[review_id] = theme_id
    return labels


def _normalize_theme_id(raw: str) -> str:
    """Turn a model slug into the id form the rest of the pipeline stores."""
    return raw.strip().casefold().replace("-", "_")


def _ground_example(phrase: str, sample_texts: list[str]) -> str | None:
    """Return a span that actually occurs in the sample, or None.

    An exact copy is kept. An ellipsis is accepted when every piece sits in
    the same review, and the longest piece is what gets stored. A one-off
    spelling fix is snapped back to the review's own words.
    """
    cleaned = phrase.strip().strip("\"'")
    if not cleaned:
        return None
    squashed = _squash(cleaned)
    if any(squashed in text for text in sample_texts):
        return cleaned
    parts = [
        part.strip().strip("\"'")
        for part in re.split(r"\s*(?:\.{3}|…)\s*", cleaned)
        if part.strip()
    ]
    if len(parts) >= 2:
        for text in sample_texts:
            if all(_squash(part) in text for part in parts):
                return max(parts, key=lambda part: len(_squash(part)))
    return _snap_near_verbatim(squashed, sample_texts)


def _snap_near_verbatim(target: str, sample_texts: list[str]) -> str | None:
    """Replace a near-copy with the sample window it almost matches."""
    from difflib import SequenceMatcher

    words = target.split()
    if len(words) < 6:
        return None
    best_ratio = 0.0
    best: str | None = None
    widths = range(max(6, len(words) - 2), len(words) + 3)
    for text in sample_texts:
        tokens = text.split()
        for width in widths:
            if width > len(tokens):
                continue
            for start in range(len(tokens) - width + 1):
                window = " ".join(tokens[start : start + width])
                ratio = SequenceMatcher(None, target, window).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best = window
    if best is not None and best_ratio >= 0.94:
        return best
    return None


def _canonical_label(label: str, allowed: set[str]) -> str | None:
    cleaned = label.strip()
    normalized = _normalize_theme_id(cleaned)
    if cleaned.casefold() == UNCLASSIFIED or normalized == UNCLASSIFIED:
        return UNCLASSIFIED
    for theme_id in allowed:
        if cleaned == theme_id or normalized == theme_id:
            return theme_id
    return None


def _extract_json(raw: str) -> object:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise ValueError("model response was not JSON") from None


def _review_lines(reviews: list[Review]) -> str:
    return "\n".join(
        json.dumps(
            {"id": review.id, "rating": review.rating, "text": review.text},
            ensure_ascii=False,
        )
        for review in reviews
    )


def _iso_week(created_at: str) -> str:
    parsed = _parse_date(created_at)
    iso = parsed.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _median_date(values: Iterable[str]) -> date:
    """Upper median, so an even count uses the more recent of the two middle dates."""
    parsed = sorted(_parse_date(value) for value in values)
    return parsed[len(parsed) // 2]


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ClusterError(f"invalid review date {value!r}") from exc


def _squash(text: str) -> str:
    return " ".join(text.casefold().split())
