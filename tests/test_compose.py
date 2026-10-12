"""Compose a pulse with a fake model. No live model and no real export."""

import json
from pathlib import Path

import pytest

from groww_pulse.checks import check_directory, write_reviews_jsonl
from groww_pulse.cluster import write_catalog
from groww_pulse.cluster import CatalogTheme
from groww_pulse.compose import POOL_LIMIT, ComposeError, compose_directory, quote_pool
from groww_pulse.models import Review, Window

APP = "com.nextbillion.groww"
WINDOW = Window(start="2026-08-03", end="2026-10-11")
WEEK = "2026-W41"


class ScriptedModel:
    def __init__(self, responder) -> None:
        self._responder = responder
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._responder(prompt, self)


def _review(
    review_id: str,
    theme_id: str,
    text: str,
    *,
    rating: int = 1,
    created_at: str = "2026-09-15",
) -> Review:
    return Review(
        id=review_id,
        store="play",
        app_id=APP,
        rating=rating,
        title="",
        text=text,
        created_at=created_at,
        theme_id=theme_id,
    )


def _sentence(word: str, count: int = 18) -> str:
    return " ".join([word] * count) + "."


def _catalog(path: Path) -> None:
    write_catalog(
        path,
        (
            CatalogTheme("orders", "Orders", "Order placement.", ("order delay", "order failed")),
            CatalogTheme("charts", "Charts", "Chart speed.", ("chart lag", "slow chart")),
            CatalogTheme("support", "Support", "Help requests.", ("customer support", "call centre")),
            CatalogTheme("statements", "Statements", "Downloaded statements.", ("tax statement", "pnl report")),
        ),
        themes=(),
        top_theme_ids=("orders", "charts", "support"),
    )


def _theme_ids(prompt: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for section in prompt.split("--- theme ")[1:]:
        theme_id, rest = section.split(" ---", 1)
        body = rest.split("reviews:\n", 1)[1]
        ids = []
        for line in body.splitlines():
            if not line.startswith("{"):
                break
            ids.append(json.loads(line)["id"])
        found[theme_id] = ids
    return found


def _review_text(prompt: str, review_id: str) -> str:
    for line in prompt.splitlines():
        if not line.startswith("{"):
            continue
        item = json.loads(line)
        if item.get("id") == review_id:
            return item["text"]
    raise AssertionError(f"missing review {review_id}")


def _response(prompt: str, summaries: dict[str, str] | None = None) -> str:
    pools = _theme_ids(prompt)
    quotes = []
    for review_ids in pools.values():
        review_id = review_ids[0]
        quotes.append(
            {"review_id": review_id, "text": _review_text(prompt, review_id)}
        )
    summary_text = summaries or {
        "orders": "Users want an order to show the price they confirmed.",
        "charts": "Users want charts to open without a lag.",
        "support": "Users want support to stay on the call.",
    }
    return json.dumps(
        {
            "summaries": [
                {"theme_id": theme_id, "text": text}
                for theme_id, text in summary_text.items()
            ],
            "quotes": quotes,
            "actions": [
                {
                    "theme_id": "orders",
                    "text": "Fix the Orders screen so a failed order shows the confirmed price.",
                },
                {
                    "theme_id": "charts",
                    "text": "Make Charts open without the lag users hit at the open.",
                },
                {
                    "theme_id": "support",
                    "text": "Answer Support calls instead of disconnecting the user.",
                },
            ],
        }
    )


def _write_run(run: Path, reviews: list[Review]) -> None:
    write_reviews_jsonl(run / "reviews.jsonl", reviews)
    _catalog(run / "catalog.json")


def test_quote_pool_prefers_complete_sentences_and_caps_at_twelve() -> None:
    preferred = [
        _review(f"p{index:02d}", "orders", _sentence("order"), rating=1 if index % 2 == 0 else 5)
        for index in range(14)
    ]
    short = _review("short", "orders", "order delay today.", rating=1)
    capped = _review("capped", "orders", "x" * 500, rating=1)
    redacted = _review("redacted", "orders", _sentence("order") + " [redacted]", rating=2)
    named = _review(
        "named",
        "orders",
        "Employees like Ansh Agarwal constantly mute and disconnect calls.",
        rating=1,
    )
    pool = quote_pool(preferred + [short, capped, redacted, named], "orders")

    assert len(pool) == POOL_LIMIT
    ids = {review.id for review in pool}
    assert "capped" not in ids
    assert "redacted" not in ids
    assert "named" not in ids
    assert "short" not in ids
    assert {review.rating for review in pool} == {1, 5}


def test_quote_pool_keeps_a_capped_review_when_it_is_the_only_one() -> None:
    capped = _review("capped", "orders", "x" * 500, rating=1)
    assert [review.id for review in quote_pool([capped], "orders")] == ["capped"]


def test_quote_pool_spreads_ratings() -> None:
    reviews = [
        _review(f"low-{index}", "orders", _sentence("order"), rating=1)
        for index in range(8)
    ] + [
        _review(f"high-{index}", "orders", _sentence("trade"), rating=5)
        for index in range(8)
    ]
    pool = quote_pool(reviews, "orders")
    assert len(pool) == POOL_LIMIT
    assert sum(review.rating == 1 for review in pool) == 6
    assert sum(review.rating == 5 for review in pool) == 6


def test_grounded_note_is_written_and_the_pool_stays_at_twelve(tmp_path: Path) -> None:
    reviews = [
        _review(f"o{index:02d}", "orders", _sentence("order"), rating=1 if index % 2 == 0 else 5)
        for index in range(13)
    ]
    reviews.extend(
        [
            _review("capped", "orders", "y" * 500, rating=1),
            _review("redacted", "orders", _sentence("order") + " [redacted]", rating=1),
            _review(
                "named",
                "orders",
                "Employees like Ansh Agarwal mute calls and disconnect.",
                rating=1,
            ),
            _review("chart", "charts", _sentence("chart"), rating=2),
            _review("help", "support", _sentence("support"), rating=1),
            _review("praise", "unclassified", _sentence("praise"), rating=5),
        ]
    )
    run = tmp_path / WEEK
    _write_run(run, reviews)

    def respond(prompt: str, _model: ScriptedModel) -> str:
        pools = _theme_ids(prompt)
        assert len(pools["orders"]) == POOL_LIMIT
        assert "capped" not in pools["orders"]
        assert "redacted" not in pools["orders"]
        assert "named" not in pools["orders"]
        assert "praise" not in {review_id for ids in pools.values() for review_id in ids}
        assert len(pools["charts"]) == 1
        assert len(pools["support"]) == 1
        return _response(prompt)

    pulse = compose_directory(
        run, ScriptedModel(respond), week=WEEK, window=WINDOW, product_name="Groww"
    )

    note = (run / "pulse.txt").read_text(encoding="utf-8")
    assert pulse.word_count == len(note.split())
    assert pulse.word_count <= 280
    assert pulse.doc_url == ""
    assert pulse.draft_id == ""
    assert pulse.review_count == len(reviews)
    assert check_directory(run) == ()
    assert note.startswith("Groww weekly pulse — 2026-W41\n")
    assert "User quotes" in note
    assert "Action ideas" in note
    assert f"{APP}" in note


def test_near_miss_quote_is_retried_and_not_publishable(tmp_path: Path) -> None:
    reviews = [
        _review("o1", "orders", _sentence("order"), rating=1),
        _review("c1", "charts", _sentence("chart"), rating=2),
        _review("s1", "support", _sentence("support"), rating=1),
    ]
    run = tmp_path / WEEK
    _write_run(run, reviews)

    def respond(prompt: str, _model: ScriptedModel) -> str:
        payload = json.loads(_response(prompt))
        payload["quotes"][0]["text"] = "this quote is not in the review"
        return json.dumps(payload)

    model = ScriptedModel(respond)
    with pytest.raises(ComposeError, match="after two retries"):
        compose_directory(run, model, week=WEEK, window=WINDOW)

    assert len(model.prompts) == 3
    assert "verbatim" in model.prompts[1]
    assert "verbatim" in model.prompts[2]
    saved = json.loads((run / "pulse.json").read_text(encoding="utf-8"))
    assert saved["doc_url"] == ""
    assert saved["draft_id"] == ""
    assert check_directory(run)


def test_personal_name_quote_is_retried_and_not_publishable(tmp_path: Path) -> None:
    named = "Employees like Ansh Agarwal constantly mute and disconnect calls."
    reviews = [
        _review("o1", "orders", named, rating=1),
        _review("c1", "charts", _sentence("chart"), rating=2),
        _review("s1", "support", _sentence("support"), rating=1),
    ]
    run = tmp_path / WEEK
    _write_run(run, reviews)

    def respond(prompt: str, _model: ScriptedModel) -> str:
        pools = _theme_ids(prompt)
        assert pools["orders"] == []
        payload = {
            "summaries": [
                {"theme_id": "orders", "text": "Users want an order to show the price they confirmed."},
                {"theme_id": "charts", "text": "Users want charts to open without a lag."},
                {"theme_id": "support", "text": "Users want support to stay on the call."},
            ],
            "quotes": [
                {
                    "review_id": "o1",
                    "text": "Employees like Ansh Agarwal constantly mute",
                },
                {
                    "review_id": "c1",
                    "text": _review_text(prompt, "c1"),
                },
                {
                    "review_id": "s1",
                    "text": _review_text(prompt, "s1"),
                },
            ],
            "actions": [
                {
                    "theme_id": "orders",
                    "text": "Fix the Orders screen so a failed order shows the confirmed price.",
                },
                {
                    "theme_id": "charts",
                    "text": "Make Charts open without the lag users hit at the open.",
                },
                {
                    "theme_id": "support",
                    "text": "Answer Support calls instead of disconnecting the user.",
                },
            ],
        }
        return json.dumps(payload)

    model = ScriptedModel(respond)
    with pytest.raises(ComposeError, match="after two retries"):
        compose_directory(run, model, week=WEEK, window=WINDOW)

    assert len(model.prompts) == 3
    assert "privacy" in model.prompts[1]
    assert "Ansh Agarwal" in model.prompts[1]
    saved = json.loads((run / "pulse.json").read_text(encoding="utf-8"))
    assert saved["doc_url"] == ""
    assert saved["draft_id"] == ""


def test_length_retry_keeps_the_quote_strings(tmp_path: Path) -> None:
    reviews = [
        _review("o1", "orders", _sentence("order"), rating=1),
        _review("c1", "charts", _sentence("chart"), rating=2),
        _review("s1", "support", _sentence("support"), rating=1),
    ]
    run = tmp_path / WEEK
    _write_run(run, reviews)
    calls = {"count": 0}

    def respond(prompt: str, _model: ScriptedModel) -> str:
        calls["count"] += 1
        if calls["count"] == 1:
            long = " ".join(["padding"] * 120)
            return _response(
                prompt,
                summaries={"orders": long, "charts": long, "support": long},
            )
        assert "Keep these quote strings intact" in prompt
        assert _sentence("order") in prompt
        return _response(prompt)

    pulse = compose_directory(run, ScriptedModel(respond), week=WEEK, window=WINDOW)

    assert calls["count"] == 2
    assert pulse.word_count <= 280
    assert check_directory(run) == ()


def test_prompt_requires_exact_theme_names_and_a_word_budget(tmp_path: Path) -> None:
    reviews = [
        _review("o1", "orders", _sentence("order"), rating=1),
        _review("c1", "charts", _sentence("chart"), rating=2),
        _review("s1", "support", _sentence("support"), rating=1),
    ]
    run = tmp_path / WEEK
    _write_run(run, reviews)

    def respond(prompt: str, _model: ScriptedModel) -> str:
        assert "contain that theme's name exactly" in prompt
        assert "- orders: Orders" in prompt
        assert "- charts: Charts" in prompt
        assert "- support: Support" in prompt
        assert "at most 16 words" in prompt
        assert "Summaries and actions together must use at most" in prompt
        return _response(prompt)

    pulse = compose_directory(run, ScriptedModel(respond), week=WEEK, window=WINDOW)
    assert pulse.word_count <= 280
    assert check_directory(run) == ()


def test_length_and_action_retry_keeps_quotes_and_names_the_theme(tmp_path: Path) -> None:
    reviews = [
        _review("o1", "orders", _sentence("order"), rating=1),
        _review("c1", "charts", _sentence("chart"), rating=2),
        _review("s1", "support", _sentence("support"), rating=1),
    ]
    run = tmp_path / WEEK
    _write_run(run, reviews)
    calls = {"count": 0}

    def respond(prompt: str, _model: ScriptedModel) -> str:
        calls["count"] += 1
        if calls["count"] == 1:
            long = " ".join(["padding"] * 120)
            payload = json.loads(
                _response(
                    prompt,
                    summaries={"orders": long, "charts": long, "support": long},
                )
            )
            for action in payload["actions"]:
                action["text"] = "Investigate the complaints in this cluster and reply faster."
            return json.dumps(payload)
        assert "Keep these quote strings intact" in prompt
        assert "by at least" in prompt
        assert "Each action must still contain its theme name exactly." in prompt
        assert _sentence("order") in prompt
        return _response(prompt)

    pulse = compose_directory(run, ScriptedModel(respond), week=WEEK, window=WINDOW)

    assert calls["count"] == 2
    assert pulse.word_count <= 280
    assert check_directory(run) == ()
