"""Cluster and rank with the local classifier. No live model and no real export."""

import json
from pathlib import Path

import pytest

from groww_pulse.checks import load_reviews_jsonl, write_reviews_jsonl
from groww_pulse.classify import (
    GENERAL_APPRECIATION,
    UNCLASSIFIED,
    classify_texts,
    fixed_catalog,
)
from groww_pulse.cluster import MAX_THEMES, ClusterError, cluster_directory
from groww_pulse.model import LiveModelClient, ModelError
from groww_pulse.models import Review

APP = "com.nextbillion.groww"
ORDERS = "orders_and_trading"
CHARTS = "charts_speed_and_stability"
SUPPORT = "customer_support"
CHARGES = "charges_and_brokerage"
PAYMENTS = "payments_withdrawals_and_mutual_funds"


def _review(review_id: str, rating: int, text: str, created_at: str) -> Review:
    return Review(
        id=review_id,
        store="play",
        app_id=APP,
        rating=rating,
        title="",
        text=text,
        created_at=created_at,
        theme_id="",
    )


def _write(run: Path, reviews: list[Review]) -> None:
    write_reviews_jsonl(run / "reviews.jsonl", reviews)


def test_fixed_catalog_has_five_themes() -> None:
    catalog = fixed_catalog()
    assert len(catalog) == MAX_THEMES
    assert [theme.id for theme in catalog] == [
        ORDERS,
        CHARTS,
        SUPPORT,
        CHARGES,
        PAYMENTS,
    ]
    assert all(len(theme.examples) == 2 for theme in catalog)


def test_classifier_sorts_product_language_and_leaves_praise_out() -> None:
    samples = [
        ("My buy order stayed pending and never got executed in the market.", ORDERS),
        ("The IPO bid I placed was rejected this morning before the open.", ORDERS),
        ("Candles on the chart froze and the app became unusable after that.", CHARTS),
        ("I opened a ticket and customer care never called me back at all.", SUPPORT),
        ("KYC verification is stuck and nobody from help will reply to me.", SUPPORT),
        ("They deducted extra brokerage on a tiny trade this week.", CHARGES),
        ("My mutual fund SIP failed and the amount was not invested.", PAYMENTS),
        ("Happy with the app overall and I have no other comment to make.", GENERAL_APPRECIATION),
        ("asdf qwer zxcv keyboard mash nonsense here now please ignore", UNCLASSIFIED),
    ]
    assert classify_texts([text for text, _label in samples]) == [
        label for _text, label in samples
    ]


def test_classifier_is_stable() -> None:
    texts = [
        "The sell order was rejected during market hours.",
        "nice app and easy to use overall",
    ]
    assert classify_texts(texts) == classify_texts(texts)


def test_catalog_larger_than_the_cap_stops_before_writing(tmp_path: Path) -> None:
    run = tmp_path / "2026-W41"
    _write(run, [_review("r1", 1, "order delay and order failed again", "2026-08-03")])

    with pytest.raises(ClusterError, match="maximum is 4"):
        cluster_directory(run, max_themes=4)

    assert not (run / "catalog.json").exists()
    assert load_reviews_jsonl(run / "reviews.jsonl")[0].theme_id == ""


def test_ranking_uses_count_then_rating_then_recency(tmp_path: Path) -> None:
    run = tmp_path / "2026-W41"
    reviews = [
        _review("a1", 1, "order delay and order failed this morning", "2026-08-03"),
        _review("a2", 1, "order delay and order failed again", "2026-08-04"),
        _review("b1", 5, "chart lag and slow chart at the open", "2026-10-07"),
        _review("b2", 5, "chart lag and slow chart after lunch", "2026-10-08"),
        _review("g1", 1, "customer support and the call centre never answered", "2026-10-01"),
        _review("d1", 1, "high charges and the brokerage fee are too high", "2026-08-10"),
        _review("u1", 5, "nice app and easy to use overall", "2026-09-01"),
        _review("u2", 5, "best app for beginners really", "2026-09-02"),
    ]
    _write(run, reviews)

    result = cluster_directory(run)

    assert result.top_theme_ids == (ORDERS, CHARTS, SUPPORT)
    by_id = {theme.id: theme for theme in result.themes}
    assert by_id[ORDERS].review_count == 2
    assert by_id[ORDERS].average_rating == 1
    assert by_id[CHARTS].review_count == 2
    assert by_id[CHARTS].average_rating == 5
    assert by_id[SUPPORT].review_count == 1
    assert by_id[CHARGES].review_count == 1
    assert UNCLASSIFIED not in by_id
    stored = load_reviews_jsonl(run / "reviews.jsonl")
    assert [review.id for review in stored] == [review.id for review in reviews]
    labels = {review.id: review.theme_id for review in stored}
    assert labels["u1"] == GENERAL_APPRECIATION
    assert labels["u2"] == GENERAL_APPRECIATION
    assert GENERAL_APPRECIATION not in by_id
    assert labels["a1"] == ORDERS
    assert labels["d1"] == CHARGES
    catalog_file = json.loads((run / "catalog.json").read_text(encoding="utf-8"))
    assert catalog_file["top_theme_ids"] == [ORDERS, CHARTS, SUPPORT]
    assert catalog_file["themes"][0]["examples"] == ["order delay", "order failed"]
    assert catalog_file["general_appreciation"]["name"] == "General Appreciation"
    assert catalog_file["general_appreciation"]["review_count"] == 2
    assert "review_count" in catalog_file["themes"][0]


def test_live_client_reads_pulse_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PULSE_MODEL", "pulse-test")
    monkeypatch.setenv("PULSE_API_KEY", "secret")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

        def read(self) -> bytes:
            return b'{"content":[{"type":"thinking","thinking":"skip"},{"type":"text","text":"ok"}]}'

    def fake_urlopen(request, timeout=60):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["api_key"] = request.get_header("X-api-key")
        captured["version"] = request.get_header("Anthropic-version")
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = LiveModelClient.from_env()

    assert client.complete("hello") == "ok"
    assert captured["body"]["model"] == "pulse-test"
    assert captured["body"]["max_tokens"] == 4096
    assert captured["body"]["thinking"] == {"type": "disabled"}
    assert captured["body"]["output_config"] == {"effort": "low"}
    assert "temperature" not in captured["body"]
    assert captured["api_key"] == "secret"
    assert captured["version"] == "2023-06-01"
    assert str(captured["url"]).endswith("/v1/messages")


def test_live_client_defaults_to_claude_haiku(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PULSE_MODEL", raising=False)
    monkeypatch.delenv("PULSE_MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("PULSE_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")

    client = LiveModelClient.from_env()

    assert client.model == "claude-haiku-5-5"
    assert client._base_url == "https://api.anthropic.com"


def test_live_client_requires_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PULSE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ModelError, match="ANTHROPIC_API_KEY"):
        LiveModelClient.from_env()
