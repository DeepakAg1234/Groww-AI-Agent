"""Compose a pulse note from the top three themes and a small quote pool.

The model sees at most 12 reviews per theme. Grounding checks accept or
reject the note. Two retries are allowed. ``doc_url`` and ``draft_id`` stay
empty until publish.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, replace
from pathlib import Path

from groww_pulse.checks import (
    LENGTH,
    PRIVACY,
    QUOTE_COUNT,
    THEME_MATCH,
    VERBATIM,
    WINDOW,
    check_pulse,
    load_reviews_jsonl,
)
from groww_pulse.model import ModelClient
from groww_pulse.models import Action, Pulse, Quote, Review, Theme, Window
from groww_pulse.privacy import REDACTED, personal_names

POOL_LIMIT = 12
PLAY_CAP = 490
WORD_MIN = 15
WORD_MAX = 80
COMPOSE_ATTEMPTS = 3
TARGET_WORDS = 250
SUMMARY_WORD_CAP = 16
ACTION_WORD_CAP = 22
# Quote rules have to be fixed by changing the quote. Length and action
# wording can be fixed while those strings stay put.
_QUOTE_FAILURES = frozenset({VERBATIM, THEME_MATCH, QUOTE_COUNT, PRIVACY, WINDOW})


class ComposeError(RuntimeError):
    """The composer did not produce a note the grounding checks accept."""


def compose_directory(
    run_dir: Path | str,
    client: ModelClient,
    *,
    week: str,
    window: Window,
    product_name: str = "Groww",
    max_words: int = 280,
    max_themes: int = 5,
    pulse_theme_count: int = 3,
    quote_count: int = 3,
    action_count: int = 3,
) -> Pulse:
    """Write ``pulse.json`` and ``pulse.txt`` when the checks pass.

    A note that still fails after two retries is saved for inspection and
    the run stops. The saved pulse is not publishable: ``doc_url`` and
    ``draft_id`` stay empty.
    """
    run = Path(run_dir)
    reviews = load_reviews_jsonl(run / "reviews.jsonl")
    if not reviews:
        raise ComposeError(f"no reviews in {run / 'reviews.jsonl'}")
    catalog, top_theme_ids = _load_catalog(run / "catalog.json")
    themes = _themes_with_counts(catalog, reviews)
    by_id = {theme.id: theme for theme in themes}
    missing = [theme_id for theme_id in top_theme_ids if theme_id not in by_id]
    if missing:
        raise ComposeError(
            "top themes missing from the catalog: " + ", ".join(missing)
        )
    highlighted = tuple(by_id[theme_id] for theme_id in top_theme_ids)
    pools = {theme.id: quote_pool(reviews, theme.id) for theme in highlighted}
    by_review = {review.id: review for review in reviews}
    prompt_args = {
        "highlighted": highlighted,
        "pools": pools,
        "product_name": product_name,
        "week": week,
        "review_count": len(reviews),
        "max_words": max_words,
        "quote_count": quote_count,
        "action_count": action_count,
    }
    prompt = _compose_prompt(**prompt_args)
    last_pulse: Pulse | None = None
    last_note = ""
    last_detail = "composer returned nothing"

    for _attempt in range(COMPOSE_ATTEMPTS):
        raw = client.complete(prompt)
        try:
            summaries, quote_rows, action_rows = _parse_compose(raw)
        except ValueError as exc:
            last_detail = str(exc)
            prompt = _compose_prompt(**prompt_args, rejection=last_detail)
            continue
        draft = _assemble_pulse(
            week=week,
            window=window,
            reviews=reviews,
            themes=themes,
            top_theme_ids=top_theme_ids,
            highlighted=highlighted,
            summaries=summaries,
            quote_rows=quote_rows,
            action_rows=action_rows,
            by_review=by_review,
        )
        note = render_note(draft, reviews, product_name=product_name)
        failures = check_pulse(
            reviews,
            draft,
            note,
            max_themes=max_themes,
            pulse_theme_count=pulse_theme_count,
            quote_count=quote_count,
            action_count=action_count,
            max_words=max_words,
        )
        counted = replace(draft, word_count=len(note.split()))
        if not failures:
            _write_pulse(run, counted, note)
            return counted
        last_pulse = counted
        last_note = note
        last_detail = "; ".join(
            f"{failure.rule}: {failure.detail}" for failure in failures
        )
        length_failed = any(failure.rule == LENGTH for failure in failures)
        quote_failed = any(failure.rule in _QUOTE_FAILURES for failure in failures)
        keep_quotes = (
            tuple(quote.text for quote in counted.quotes)
            if length_failed and not quote_failed
            else ()
        )
        prompt = _compose_prompt(
            **prompt_args,
            rejection=last_detail,
            rejected_note=last_note,
            keep_quotes=keep_quotes,
            words_to_cut=max(0, counted.word_count - max_words) if length_failed else 0,
        )

    if last_pulse is not None:
        _write_pulse(run, last_pulse, last_note)
    raise ComposeError(f"compose failed after two retries: {last_detail}")


def quote_pool(
    reviews: list[Review] | tuple[Review, ...], theme_id: str
) -> list[Review]:
    """Pick at most 12 reviews for one theme.

    Bodies that contain ``[redacted]`` or a personal name are left out.
    A review cut at the Play character cap is left out when the theme still
    has a complete alternative. Prefer 15–80 words that end on a sentence,
    and spread the pool across star ratings.
    """
    eligible = [
        review
        for review in reviews
        if review.theme_id == theme_id and not _blocked(review.text)
    ]
    complete = [review for review in eligible if len(review.text) < PLAY_CAP]
    if complete:
        eligible = complete
    preferred = [review for review in eligible if _preferred(review.text)]
    preferred_ids = {review.id for review in preferred}
    remainder = [review for review in eligible if review.id not in preferred_ids]
    picked = _spread_ratings(preferred, POOL_LIMIT)
    if len(picked) < POOL_LIMIT:
        picked.extend(_spread_ratings(remainder, POOL_LIMIT - len(picked)))
    return picked


def render_note(
    pulse: Pulse,
    reviews: list[Review] | tuple[Review, ...],
    *,
    product_name: str,
) -> str:
    """Render the note in the order from the architecture."""
    by_theme = {theme.id: theme for theme in pulse.themes}
    by_review = {review.id: review for review in reviews}
    highlighted = [
        by_theme[theme_id]
        for theme_id in pulse.top_theme_ids
        if theme_id in by_theme
    ]
    share = sum(theme.review_count for theme in highlighted)
    lines = [
        f"{product_name} weekly pulse — {pulse.week}",
        "",
        _volume_sentence(pulse.review_count, share),
        "",
        "Top themes",
    ]
    for theme in highlighted:
        lines.append(_theme_sentence(theme))
    lines.extend(["", "User quotes"])
    for quote in pulse.quotes:
        review = by_review.get(quote.review_id)
        package = review.app_id if review is not None else quote.store
        lines.append(f'"{quote.text}" — {quote.rating} stars, {package}')
    lines.extend(["", "Action ideas"])
    for action in pulse.actions:
        lines.append(action.text)
    return "\n".join(lines) + "\n"


def _compose_prompt(
    *,
    highlighted: tuple[Theme, ...],
    pools: dict[str, list[Review]],
    product_name: str,
    week: str,
    review_count: int,
    max_words: int,
    quote_count: int,
    action_count: int,
    rejection: str = "",
    rejected_note: str = "",
    keep_quotes: tuple[str, ...] = (),
    words_to_cut: int = 0,
) -> str:
    sections: list[str] = []
    for theme in highlighted:
        pool = pools.get(theme.id, [])
        sections.append(
            "\n".join(
                [
                    f"--- theme {theme.id} ---",
                    f"name: {theme.name}",
                    f"review_count: {theme.review_count}",
                    f"average_rating: {theme.average_rating:.1f}",
                    "reviews:",
                    _review_lines(pool),
                ]
            )
        )
    summary_cap, action_cap, budget = _writing_limits(
        highlighted=highlighted,
        pools=pools,
        product_name=product_name,
        week=week,
        review_count=review_count,
        max_words=max_words,
        quote_count=quote_count,
        action_count=action_count,
    )
    names = "\n".join(f"- {theme.id}: {theme.name}" for theme in highlighted)
    retry = ""
    if rejection:
        retry = f"\nThe previous note was rejected: {rejection}\n"
        if keep_quotes:
            quoted = "\n".join(f"- {text}" for text in keep_quotes)
            cut = ""
            if words_to_cut:
                cut = (
                    f"Shorten the summaries and actions by at least "
                    f"{words_to_cut} words. "
                )
            retry += (
                "Keep these quote strings intact and shorten only the summaries "
                f"and actions. {cut}"
                "Each action must still contain its theme name exactly.\n"
                f"{quoted}\n"
            )
        if rejected_note:
            retry += f"\nRejected note:\n{rejected_note}\n"
    return (
        "TASK: compose\n"
        f"Write a {product_name} weekly pulse for {week}.\n"
        f"Aim for about {TARGET_WORDS} words. Stay at or under {max_words} words.\n"
        f"Return exactly {quote_count} quotes and exactly {action_count} actions.\n"
        "Prefer one quote from each highlighted theme. Copy each quote as a "
        "contiguous span of that review's stored text. A quote may be the entire "
        "short review. It may contain ₹ and other characters already in the text. "
        "Do not finish a sentence the Play cap cut off. Do not include a person's "
        "name or the token [redacted].\n"
        f"Each summary is one sentence on what users are asking for, "
        f"at most {summary_cap} words.\n"
        f"Each action is one sentence of at most {action_cap} words. It states a "
        "product, support, or messaging next step grounded in that theme's reviews. "
        "The action text must contain that theme's name exactly, as a contiguous "
        "phrase, matching this list:\n"
        f"{names}\n"
        "Do not invent metrics, ship dates, or quotes.\n"
        "The rendered note counts the title, volume line, theme counts, quotes, "
        "attributions, summaries, and actions. Summaries and actions together "
        f"must use at most {budget} words.\n"
        f"{retry}\n"
        "Respond with JSON only:\n"
        '{"summaries": [{"theme_id": "...", "text": "..."}], '
        '"quotes": [{"review_id": "...", "text": "..."}], '
        '"actions": [{"theme_id": "...", "text": "..."}]}\n\n'
        "Highlighted themes:\n"
        + "\n\n".join(sections)
        + "\n"
    )


def _writing_limits(
    *,
    highlighted: tuple[Theme, ...],
    pools: dict[str, list[Review]],
    product_name: str,
    week: str,
    review_count: int,
    max_words: int,
    quote_count: int,
    action_count: int,
) -> tuple[int, int, int]:
    """Word caps that leave room for the frame and a typical quote from each theme."""
    frame = _frame_word_count(
        product_name=product_name,
        week=week,
        review_count=review_count,
        highlighted=highlighted,
    )
    attribution = quote_count * _attribution_words(_package(pools, highlighted))
    reserve = _quote_body_reserve(highlighted, pools, quote_count)
    budget = max_words - frame - attribution - reserve
    name_words = max((len(theme.name.split()) for theme in highlighted), default=1)
    action_cap = min(ACTION_WORD_CAP, max(name_words + 6, 12))
    summary_cap = SUMMARY_WORD_CAP
    minimum = summary_cap * quote_count + action_cap * action_count
    if budget < minimum:
        budget = minimum
    return summary_cap, action_cap, budget


def _frame_word_count(
    *,
    product_name: str,
    week: str,
    review_count: int,
    highlighted: tuple[Theme, ...],
) -> int:
    share = sum(theme.review_count for theme in highlighted)
    parts = [
        f"{product_name} weekly pulse — {week}",
        _volume_sentence(review_count, share),
        "Top themes",
    ]
    for theme in highlighted:
        parts.append(_theme_sentence(replace(theme, summary="")))
    parts.extend(["User quotes", "Action ideas"])
    return len(" ".join(parts).split())


def _quote_body_reserve(
    highlighted: tuple[Theme, ...],
    pools: dict[str, list[Review]],
    quote_count: int,
) -> int:
    reserve = 0
    for theme in highlighted[:quote_count]:
        pool = pools.get(theme.id, [])
        if not pool:
            reserve += 20
            continue
        lengths = sorted(len(review.text.split()) for review in pool)
        reserve += lengths[len(lengths) // 2]
    return reserve


def _package(pools: dict[str, list[Review]], highlighted: tuple[Theme, ...]) -> str:
    for theme in highlighted:
        for review in pools.get(theme.id, []):
            return review.app_id
    return "play"


def _attribution_words(package: str) -> int:
    sample = f'"word" — 1 stars, {package}'
    return len(sample.split()) - 1


def _assemble_pulse(
    *,
    week: str,
    window: Window,
    reviews: list[Review],
    themes: tuple[Theme, ...],
    top_theme_ids: tuple[str, ...],
    highlighted: tuple[Theme, ...],
    summaries: dict[str, str],
    quote_rows: list[tuple[str, str]],
    action_rows: list[tuple[str, str]],
    by_review: dict[str, Review],
) -> Pulse:
    highlighted_ids = {theme.id for theme in highlighted}
    filled: list[Theme] = []
    for theme in themes:
        summary = summaries.get(theme.id, "") if theme.id in highlighted_ids else ""
        filled.append(replace(theme, summary=summary))
    quotes: list[Quote] = []
    for review_id, text in quote_rows:
        review = by_review.get(review_id)
        quotes.append(
            Quote(
                text=text,
                rating=review.rating if review is not None else 0,
                store=review.store if review is not None else "play",
                theme_id=review.theme_id if review is not None else "",
                review_id=review_id,
            )
        )
    actions = tuple(
        Action(theme_id=theme_id, text=text) for theme_id, text in action_rows
    )
    return Pulse(
        week=week,
        window=window,
        review_count=len(reviews),
        themes=tuple(filled),
        top_theme_ids=top_theme_ids,
        quotes=tuple(quotes),
        actions=actions,
        word_count=0,
        doc_url="",
        draft_id="",
    )


def _parse_compose(
    raw: str,
) -> tuple[dict[str, str], list[tuple[str, str]], list[tuple[str, str]]]:
    payload = _extract_json(raw)
    if not isinstance(payload, dict):
        raise ValueError("compose response must be a JSON object")
    summaries = _pairs(payload.get("summaries"), "summary", "theme_id")
    quotes = _pairs(payload.get("quotes"), "quote", "review_id")
    actions = _pairs(payload.get("actions"), "action", "theme_id")
    return dict(summaries), quotes, actions


def _pairs(value: object, label: str, id_key: str) -> list[tuple[str, str]]:
    if not isinstance(value, list):
        raise ValueError(f"compose response did not include {label}s")
    rows: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"each {label} must be a JSON object")
        item_id = str(item.get(id_key, "")).strip()
        text = str(item.get("text", "")).strip()
        if not item_id or not text:
            raise ValueError(f"each {label} needs a {id_key} and text")
        rows.append((item_id, text))
    return rows


def _load_catalog(path: Path) -> tuple[tuple[Theme, ...], tuple[str, ...]]:
    if not path.is_file():
        raise ComposeError(f"missing catalog: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ComposeError("catalog.json must be an object")
    items = raw.get("themes")
    top = raw.get("top_theme_ids")
    if not isinstance(items, list) or not items:
        raise ComposeError("catalog.json did not include themes")
    if not isinstance(top, list) or not top:
        raise ComposeError("catalog.json did not include top_theme_ids")
    themes: list[Theme] = []
    for item in items:
        if not isinstance(item, dict):
            raise ComposeError("each catalog theme must be an object")
        theme_id = str(item.get("id", "")).strip()
        name = str(item.get("name", "")).strip()
        if not theme_id or not name:
            raise ComposeError("each catalog theme needs an id and a name")
        themes.append(
            Theme(
                id=theme_id,
                name=name,
                definition=str(item.get("definition", "")).strip(),
            )
        )
    return tuple(themes), tuple(str(theme_id) for theme_id in top)


def _themes_with_counts(
    catalog: tuple[Theme, ...], reviews: list[Review]
) -> tuple[Theme, ...]:
    grouped: dict[str, list[Review]] = {theme.id: [] for theme in catalog}
    for review in reviews:
        if review.theme_id in grouped:
            grouped[review.theme_id].append(review)
    counted: list[Theme] = []
    for theme in catalog:
        members = grouped[theme.id]
        average = (
            sum(review.rating for review in members) / len(members) if members else 0.0
        )
        counted.append(
            replace(
                theme,
                review_count=len(members),
                average_rating=average,
            )
        )
    return tuple(counted)


def _spread_ratings(reviews: list[Review], limit: int) -> list[Review]:
    if limit <= 0 or not reviews:
        return []
    by_rating: dict[int, list[Review]] = {rating: [] for rating in range(1, 6)}
    for review in reviews:
        if review.rating in by_rating:
            by_rating[review.rating].append(review)
    for bucket in by_rating.values():
        bucket.sort(key=lambda review: review.id)
    picked: list[Review] = []
    index = dict.fromkeys(range(1, 6), 0)
    while len(picked) < limit:
        progressed = False
        for rating in range(1, 6):
            cursor = index[rating]
            bucket = by_rating[rating]
            if cursor >= len(bucket):
                continue
            picked.append(bucket[cursor])
            index[rating] = cursor + 1
            progressed = True
            if len(picked) == limit:
                break
        if not progressed:
            break
    return picked


def _blocked(text: str) -> bool:
    return REDACTED.casefold() in text.casefold() or bool(personal_names(text))


def _preferred(text: str) -> bool:
    words = len(text.split())
    return WORD_MIN <= words <= WORD_MAX and text.rstrip().endswith((".", "!", "?"))


def _volume_sentence(total: int, share: int) -> str:
    total_unit = "review" if total == 1 else "reviews"
    verb = "sits" if share == 1 else "sit"
    return (
        f"This window holds {total} {total_unit}, and {share} of them "
        f"{verb} in the top three themes."
    )


def _theme_sentence(theme: Theme) -> str:
    unit = "review" if theme.review_count == 1 else "reviews"
    summary = theme.summary.strip()
    if summary and not summary.endswith((".", "!", "?")):
        summary = summary + "."
    base = (
        f"{theme.name} had {theme.review_count} {unit} at an average rating "
        f"of {theme.average_rating:.1f}."
    )
    if summary:
        return f"{base} {summary}"
    return base


def _review_lines(reviews: list[Review]) -> str:
    if not reviews:
        return ""
    return "\n".join(
        json.dumps(
            {"id": review.id, "rating": review.rating, "text": review.text},
            ensure_ascii=False,
        )
        for review in reviews
    )


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
        raise ValueError("compose response was not JSON") from None


def _write_pulse(run: Path, pulse: Pulse, note: str) -> None:
    run.mkdir(parents=True, exist_ok=True)
    (run / "pulse.json").write_text(
        json.dumps(asdict(pulse), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (run / "pulse.txt").write_text(note, encoding="utf-8")
