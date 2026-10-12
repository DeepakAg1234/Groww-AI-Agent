"""Cluster and rank with a fake model. No live model and no real export."""

import json
from datetime import date
from pathlib import Path

import pytest

from groww_pulse.checks import load_reviews_jsonl, write_reviews_jsonl
from groww_pulse.cluster import (
    BATCH_SIZE,
    SEED_HINTS,
    ClusterError,
    cluster_directory,
    sample_reviews,
)
from groww_pulse.model import LiveModelClient, ModelError
from groww_pulse.models import Review

APP = "com.nextbillion.groww"


class ScriptedModel:
    def __init__(self, responder, run: Path | None = None) -> None:
        self._responder = responder
        self.run = run
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._responder(prompt, self)


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


def _catalog_json(themes: list[dict]) -> str:
    return json.dumps({"themes": themes})


def _assignments(pairs: list[tuple[str, str]]) -> str:
    return json.dumps(
        {"assignments": [{"id": review_id, "theme_id": theme_id} for review_id, theme_id in pairs]}
    )


def _review_ids(prompt: str) -> list[str]:
    body = prompt.split("Reviews:\n", 1)[1]
    return [json.loads(line)["id"] for line in body.splitlines() if line.strip()]


def _task(prompt: str) -> str:
    return prompt.splitlines()[0]


def test_sample_is_stratified_and_spread_across_weeks() -> None:
    reviews: list[Review] = []
    for week in range(32, 42):
        day = date.fromisocalendar(2026, week, 1).isoformat()
        for index in range(3):
            reviews.append(_review(f"low-{week}-{index}", 1, "order delay", day))
        for index in range(2):
            reviews.append(_review(f"mid-{week}-{index}", 2 if index == 0 else 3, "chart lag", day))
        for index in range(3):
            reviews.append(_review(f"high-{week}-{index}", 5, "nice detail", day))

    sample = sample_reviews(reviews)
    again = sample_reviews(reviews)

    assert [review.id for review in sample] == [review.id for review in again]
    assert len(sample) == 60
    assert sum(review.rating == 1 for review in sample) == 24
    assert sum(review.rating in (2, 3) for review in sample) == 12
    assert sum(review.rating >= 4 for review in sample) == 24
    low_weeks = {review.created_at for review in sample if review.rating == 1}
    assert len(low_weeks) == 10


def test_six_theme_catalog_fails_after_one_retry(tmp_path: Path) -> None:
    run = tmp_path / "2026-W41"
    _write(run, [_review("r1", 1, "order delay and order failed", "2026-08-03")])
    themes = [{"id": f"theme{index}", "name": f"Theme {index}"} for index in range(6)]

    def respond(prompt: str, _model: ScriptedModel) -> str:
        assert _task(prompt) == "TASK: catalog"
        return _catalog_json(themes)

    model = ScriptedModel(respond, run)
    with pytest.raises(ClusterError, match="after one retry"):
        cluster_directory(run, model)

    assert len(model.prompts) == 2
    assert "6 themes" in model.prompts[1]
    assert not (run / "catalog.json").exists()
    assert load_reviews_jsonl(run / "reviews.jsonl")[0].theme_id == ""


def test_catalog_retry_accepts_a_valid_second_response(tmp_path: Path) -> None:
    run = tmp_path / "2026-W41"
    _write(run, [_review("r1", 1, "order delay and order failed today", "2026-08-03")])
    calls = {"catalog": 0}

    def respond(prompt: str, _model: ScriptedModel) -> str:
        if _task(prompt) == "TASK: catalog":
            calls["catalog"] += 1
            if calls["catalog"] == 1:
                return _catalog_json([{"id": f"t{index}"} for index in range(6)])
            return _catalog_json(
                [
                    {
                        "id": "orders",
                        "name": "Orders and trading",
                        "definition": "Order placement problems.",
                        "examples": ["order delay", "order failed"],
                    }
                ]
            )
        review_id = _review_ids(prompt)[0]
        return _assignments([(review_id, "orders")])

    result = cluster_directory(run, ScriptedModel(respond, run))

    assert calls["catalog"] == 2
    assert result.top_theme_ids == ("orders",)
    assert result.reviews[0].theme_id == "orders"


def test_four_theme_catalog_ranks_with_rating_tie_break(tmp_path: Path) -> None:
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
    catalog = [
        {
            "id": "alpha",
            "name": "Orders and trading",
            "definition": "Order placement problems.",
            "examples": ["order delay", "order failed"],
        },
        {
            "id": "beta",
            "name": "Charts and speed",
            "definition": "Charts and app speed.",
            "examples": ["chart lag", "slow chart"],
        },
        {
            "id": "gamma",
            "name": "Customer support",
            "definition": "Help requests that go unanswered.",
            "examples": ["customer support", "call centre"],
        },
        {
            "id": "delta",
            "name": "Charges",
            "definition": "Brokerage and other charges.",
            "examples": ["high charges", "brokerage fee"],
        },
    ]
    labels = {
        "a1": "alpha",
        "a2": "alpha",
        "b1": "beta",
        "b2": "beta",
        "g1": "gamma",
        "d1": "delta",
        "u1": "unclassified",
        "u2": "unclassified",
    }

    def respond(prompt: str, model: ScriptedModel) -> str:
        if _task(prompt) == "TASK: catalog":
            assert all(seed in prompt for seed in SEED_HINTS)
            assert "KYC" in prompt
            return _catalog_json(catalog)
        assert _task(prompt) == "TASK: assign"
        assert "Seed hints" not in prompt
        assert model.run is not None
        frozen = json.loads((model.run / "catalog.json").read_text(encoding="utf-8"))
        assert "top_theme_ids" not in frozen
        assert "review_count" not in frozen["themes"][0]
        assert [theme["id"] for theme in frozen["themes"]] == [
            "alpha",
            "beta",
            "gamma",
            "delta",
        ]
        returned = [(review_id, labels[review_id]) for review_id in _review_ids(prompt)]
        return _assignments(returned)

    result = cluster_directory(run, ScriptedModel(respond, run))

    assert result.top_theme_ids == ("alpha", "beta", "gamma")
    by_id = {theme.id: theme for theme in result.themes}
    assert by_id["alpha"].review_count == 2
    assert by_id["alpha"].average_rating == 1
    assert by_id["beta"].review_count == 2
    assert by_id["beta"].average_rating == 5
    assert "unclassified" not in by_id
    stored = load_reviews_jsonl(run / "reviews.jsonl")
    assert [review.id for review in stored] == [review.id for review in reviews]
    assert {review.id: review.theme_id for review in stored} == labels
    catalog_file = json.loads((run / "catalog.json").read_text(encoding="utf-8"))
    assert catalog_file["top_theme_ids"] == ["alpha", "beta", "gamma"]
    assert catalog_file["themes"][0]["examples"] == ["order delay", "order failed"]


def test_assignment_retries_a_bad_label_once_per_review(tmp_path: Path) -> None:
    run = tmp_path / "2026-W41"
    reviews = [
        _review(f"{index:03d}", 1, "order delay and order failed", "2026-08-03")
        for index in range(BATCH_SIZE + 1)
    ]
    _write(run, reviews)
    catalog = [
        {
            "id": "alpha",
            "name": "Orders and trading",
            "definition": "Order placement problems.",
            "examples": ["order delay", "order failed"],
        }
    ]
    assign_calls = {"count": 0}

    def respond(prompt: str, _model: ScriptedModel) -> str:
        if _task(prompt) == "TASK: catalog":
            return _catalog_json(catalog)
        assign_calls["count"] += 1
        review_ids = _review_ids(prompt)
        if assign_calls["count"] == 1:
            assert len(review_ids) == BATCH_SIZE
            pairs = []
            for review_id in review_ids:
                if review_id == "000":
                    continue
                theme_id = "nope" if review_id == "001" else "alpha"
                pairs.append((review_id, theme_id))
            return _assignments(pairs)
        if assign_calls["count"] == 2:
            assert review_ids == ["000", "001"]
            assert "missing" in prompt
            assert "outside the catalog" in prompt
            return _assignments([("000", "alpha"), ("001", "alpha")])
        assert review_ids == ["030"]
        return _assignments([("030", "alpha")])

    result = cluster_directory(run, ScriptedModel(respond, run))

    assert assign_calls["count"] == 3
    assert {review.theme_id for review in result.reviews} == {"alpha"}
    assert len(result.reviews) == BATCH_SIZE + 1


def test_assignment_failure_leaves_reviews_unlabeled(tmp_path: Path) -> None:
    run = tmp_path / "2026-W41"
    _write(run, [_review("r1", 1, "order delay and order failed", "2026-08-03")])

    def respond(prompt: str, _model: ScriptedModel) -> str:
        if _task(prompt) == "TASK: catalog":
            return _catalog_json(
                [
                    {
                        "id": "alpha",
                        "name": "Orders",
                        "definition": "Order placement problems.",
                        "examples": ["order delay", "order failed"],
                    }
                ]
            )
        return _assignments([("r1", "missing-theme")])

    with pytest.raises(ClusterError, match="after one retry"):
        cluster_directory(run, ScriptedModel(respond, run))

    assert (run / "catalog.json").exists()
    frozen = json.loads((run / "catalog.json").read_text(encoding="utf-8"))
    assert "top_theme_ids" not in frozen
    assert load_reviews_jsonl(run / "reviews.jsonl")[0].theme_id == ""


def test_truncated_assignment_keeps_complete_labels(tmp_path: Path) -> None:
    run = tmp_path / "2026-W41"
    reviews = [
        _review("aaaa", 1, "order delay and order failed", "2026-08-03"),
        _review("bbbb", 1, "chart lag and slow chart", "2026-08-04"),
    ]
    _write(run, reviews)
    calls = {"assign": 0}

    def respond(prompt: str, _model: ScriptedModel) -> str:
        if _task(prompt) == "TASK: catalog":
            return _catalog_json(
                [
                    {
                        "id": "orders",
                        "name": "Orders",
                        "definition": "Order placement problems.",
                        "examples": ["order delay", "order failed"],
                    }
                ]
            )
        calls["assign"] += 1
        if calls["assign"] == 1:
            return (
                '{"assignments": ['
                '{"id": "aaaa", "theme_id": "orders"},'
                '{"id": "bbbb", "theme_id": "ord'
            )
        return _assignments([("bbbb", "orders")])

    result = cluster_directory(run, ScriptedModel(respond, run))

    assert calls["assign"] == 2
    assert {review.id: review.theme_id for review in result.reviews} == {
        "aaaa": "orders",
        "bbbb": "orders",
    }


def test_catalog_grounds_hyphen_ids_and_near_verbatim_examples(tmp_path: Path) -> None:
    support = (
        "0 customer support i made withdrawal request from my demat account "
        "amount deducted from my demat account but not credited in bank account "
        "when I raised ticket no response gave option to chat excutive"
    )
    trading = (
        "The reason behind giving it one star is today for intra day trade "
        "i purchased muthoot finance shares I was in good profit and market "
        "closes at 3:20 it squared it off automatically at 3189"
    )
    run = tmp_path / "2026-W41"
    _write(
        run,
        [
            _review("r1", 1, support, "2026-08-03"),
            _review("r2", 1, trading, "2026-08-04"),
        ],
    )

    def respond(prompt: str, _model: ScriptedModel) -> str:
        if _task(prompt) == "TASK: catalog":
            return _catalog_json(
                [
                    {
                        "id": "customer-support",
                        "name": "Customer support",
                        "definition": "Help requests that go unanswered.",
                        "examples": [
                            support.replace("excutive", "executive"),
                            (
                                "The reason behind giving it one star is today "
                                "for intra day trade ... it squared it off "
                                "automatically at 3189"
                            ),
                        ],
                    }
                ]
            )
        return _assignments(
            [(review_id, "customer-support") for review_id in _review_ids(prompt)]
        )

    result = cluster_directory(run, ScriptedModel(respond, run))

    catalog = json.loads((run / "catalog.json").read_text(encoding="utf-8"))
    theme = catalog["themes"][0]
    assert theme["id"] == "customer_support"
    assert "excutive" in theme["examples"][0]
    assert "executive" not in theme["examples"][0]
    assert theme["examples"][1] == (
        "The reason behind giving it one star is today for intra day trade"
    )
    assert {review.theme_id for review in result.reviews} == {"customer_support"}


def test_invented_example_phrase_still_fails(tmp_path: Path) -> None:
    run = tmp_path / "2026-W41"
    _write(run, [_review("r1", 1, "order delay and order failed today", "2026-08-03")])

    def respond(prompt: str, _model: ScriptedModel) -> str:
        return _catalog_json(
            [
                {
                    "id": "orders",
                    "name": "Orders",
                    "definition": "Order placement problems.",
                    "examples": [
                        "order delay",
                        "this phrase was never written in any review body",
                    ],
                }
            ]
        )

    with pytest.raises(ClusterError, match="does not appear"):
        cluster_directory(run, ScriptedModel(respond, run))


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
