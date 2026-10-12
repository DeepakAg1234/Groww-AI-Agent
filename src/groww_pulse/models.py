"""Review, Theme, and Pulse records for one weekly run."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Review:
    id: str
    store: str
    app_id: str
    rating: int
    title: str
    text: str
    created_at: str
    theme_id: str = ""


@dataclass(frozen=True)
class Theme:
    id: str
    name: str
    definition: str
    review_count: int = 0
    average_rating: float = 0.0
    summary: str = ""


@dataclass(frozen=True)
class Quote:
    text: str
    rating: int
    store: str
    theme_id: str
    review_id: str


@dataclass(frozen=True)
class Action:
    theme_id: str
    text: str


@dataclass(frozen=True)
class Window:
    start: str
    end: str


@dataclass(frozen=True)
class Pulse:
    week: str
    window: Window
    review_count: int
    themes: tuple[Theme, ...]
    top_theme_ids: tuple[str, ...]
    quotes: tuple[Quote, ...]
    actions: tuple[Action, ...]
    word_count: int = 0
    doc_url: str = ""
    draft_id: str = ""
