"""Label every review with the fixed theme catalog, then rank the top three.

Reads ``reviews.jsonl`` only. The raw Play export is not opened again.
Labels come from the local classifier, not from the language model.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from groww_pulse.checks import load_reviews_jsonl, write_reviews_jsonl
from groww_pulse.classify import (
    GENERAL_APPRECIATION,
    UNCLASSIFIED,
    CatalogTheme,
    classify_texts,
    fixed_catalog,
)
from groww_pulse.models import Review, Theme

MAX_THEMES = 5
PULSE_THEME_COUNT = 3


class ClusterError(RuntimeError):
    """Catalog or assignment could not be accepted."""


@dataclass(frozen=True)
class ClusterResult:
    themes: tuple[Theme, ...]
    top_theme_ids: tuple[str, ...]
    reviews: tuple[Review, ...]


def cluster_directory(
    run_dir: Path | str,
    *,
    max_themes: int = MAX_THEMES,
    pulse_theme_count: int = PULSE_THEME_COUNT,
) -> ClusterResult:
    """Label one run folder and write ``catalog.json`` plus theme ids.

    The catalog is the fixed five-theme list. It is written before labels
    are stored. Theme ids are written back to ``reviews.jsonl`` only after
    every review has a label.
    """
    run = Path(run_dir)
    reviews_path = run / "reviews.jsonl"
    reviews = load_reviews_jsonl(reviews_path)
    if not reviews:
        raise ClusterError(f"no reviews in {reviews_path}")

    catalog = fixed_catalog()
    if len(catalog) > max_themes:
        raise ClusterError(
            f"catalog has {len(catalog)} themes; the maximum is {max_themes}"
        )
    catalog_path = run / "catalog.json"
    write_catalog(catalog_path, catalog)
    labels = {
        review.id: theme_id
        for review, theme_id in zip(
            reviews, classify_texts([review.text for review in reviews]), strict=True
        )
    }
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
        appreciation_count=sum(
            1 for theme_id in labels.values() if theme_id == GENERAL_APPRECIATION
        ),
    )
    return ClusterResult(
        themes=ranked,
        top_theme_ids=top_theme_ids,
        reviews=tuple(labeled),
    )


def rank_themes(
    catalog: tuple[CatalogTheme, ...],
    reviews: list[Review] | tuple[Review, ...],
    labels: dict[str, str],
) -> tuple[Theme, ...]:
    """Order themes by count, then sharper pain, then a more recent median date.

    ``unclassified`` and ``general_appreciation`` are stored on reviews and
    do not enter this order.
    """
    by_id = {review.id: review for review in reviews}
    grouped: dict[str, list[Review]] = {theme.id: [] for theme in catalog}
    for review_id, theme_id in labels.items():
        if theme_id in {UNCLASSIFIED, GENERAL_APPRECIATION}:
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
    appreciation_count: int | None = None,
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
    if appreciation_count is not None:
        body["general_appreciation"] = {
            "id": GENERAL_APPRECIATION,
            "name": "General Appreciation",
            "review_count": appreciation_count,
        }
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(body, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _median_date(values: Iterable[str]) -> date:
    """Upper median, so an even count uses the more recent of the two middle dates."""
    parsed = sorted(_parse_date(value) for value in values)
    return parsed[len(parsed) // 2]


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ClusterError(f"invalid review date {value!r}") from exc
