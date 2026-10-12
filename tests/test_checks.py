"""Grounding checks against hand-written pulses. No model and no MCP."""

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from groww_pulse.checks import check_directory
from groww_pulse.models import Action, Pulse, Quote, Review, Theme, Window

APP = "com.nextbillion.groww"
WINDOW = Window(start="2026-08-03", end="2026-10-11")

KYC_TEXT = "KYC verification asked for the same documents three times this month"
PAY_TEXT = "Payments failed twice while I was adding money to my wallet yesterday"
WD_TEXT = "Withdrawals stayed pending for five days after the request was raised"

KYC_QUOTE = "KYC  verification asked for the same documents three times"
PAY_QUOTE = "Payments failed twice while I was adding money"
WD_QUOTE = "Withdrawals stayed pending for five days"

THEMES = (
    Theme(
        id="kyc",
        name="KYC",
        definition="Identity verification and document review.",
        review_count=1,
        average_rating=2.0,
        summary="Users want document checks to finish in one pass.",
    ),
    Theme(
        id="payments",
        name="Payments",
        definition="Adding money and failed transfers.",
        review_count=1,
        average_rating=1.0,
        summary="Users want a failed transfer to be explained immediately.",
    ),
    Theme(
        id="withdrawals",
        name="Withdrawals",
        definition="Pending payouts.",
        review_count=1,
        average_rating=2.0,
        summary="Users want a pending payout to show a clear arrival time.",
    ),
)
STATEMENTS = Theme(
    id="statements",
    name="Statements",
    definition="Downloaded account statements.",
    review_count=1,
    average_rating=3.0,
    summary="Users want a statement they can download.",
)
EXTRA_THEMES = (
    Theme(id="onboarding", name="Onboarding", definition="First-run setup."),
    Theme(id="support", name="Support", definition="Help requests."),
    Theme(id="refunds", name="Refunds", definition="Money sent back."),
)

ACTIONS = (
    Action(
        theme_id="kyc",
        text="Fix the KYC document loop so users are not asked to upload the same file again.",
    ),
    Action(
        theme_id="payments",
        text="Show a clear Payments error when money is added and the transfer fails.",
    ),
    Action(
        theme_id="withdrawals",
        text="Tell users why Withdrawals stay pending and when the amount will arrive.",
    ),
)


def _review(
    review_id: str,
    theme_id: str,
    text: str,
    *,
    rating: int,
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


def _reviews() -> list[Review]:
    return [
        _review("rev-kyc", "kyc", KYC_TEXT, rating=2, created_at="2026-09-15"),
        _review("rev-pay", "payments", PAY_TEXT, rating=1, created_at="2026-09-20"),
        _review("rev-wd", "withdrawals", WD_TEXT, rating=2, created_at="2026-10-01"),
    ]


def _quotes() -> tuple[Quote, ...]:
    return (
        Quote(KYC_QUOTE, 2, "play", "kyc", "rev-kyc"),
        Quote(PAY_QUOTE, 1, "play", "payments", "rev-pay"),
        Quote(WD_QUOTE, 2, "play", "withdrawals", "rev-wd"),
    )


def _pulse(
    *,
    themes: tuple[Theme, ...] = THEMES,
    quotes: tuple[Quote, ...] | None = None,
    actions: tuple[Action, ...] = ACTIONS,
) -> Pulse:
    return Pulse(
        week="2026-W41",
        window=WINDOW,
        review_count=3,
        themes=themes,
        top_theme_ids=("kyc", "payments", "withdrawals"),
        quotes=_quotes() if quotes is None else quotes,
        actions=actions,
        word_count=0,
    )


def _note(quotes: tuple[Quote, ...], extra: str = "") -> str:
    lines = [
        "Groww weekly pulse — 2026-W41",
        "",
        "This window holds 3 reviews, and all of them sit in the top three themes.",
        "",
        "Top themes",
        "KYC had 1 review at an average rating of 2.0. Users want document checks to finish in one pass.",
        "Payments had 1 review at an average rating of 1.0. Users want a failed transfer to be explained immediately.",
        "Withdrawals had 1 review at an average rating of 2.0. Users want a pending payout to show a clear arrival time.",
        "",
        "User quotes",
    ]
    for quote in quotes:
        lines.append(f'"{quote.text}" — {quote.rating} stars, {APP}')
    lines.extend(
        [
            "",
            "Action ideas",
            ACTIONS[0].text,
            ACTIONS[1].text,
            ACTIONS[2].text,
        ]
    )
    if extra:
        lines.extend(["", extra])
    return "\n".join(lines) + "\n"


def _write(
    tmp_path: Path,
    reviews: list[Review],
    pulse: Pulse,
    note: str,
) -> Path:
    run = tmp_path / "run"
    run.mkdir()
    payload = "".join(json.dumps(asdict(review)) + "\n" for review in reviews)
    (run / "reviews.jsonl").write_text(payload, encoding="utf-8")
    (run / "pulse.json").write_text(
        json.dumps(asdict(pulse), indent=2) + "\n", encoding="utf-8"
    )
    (run / "pulse.txt").write_text(note, encoding="utf-8")
    return run


def _rules(tmp_path: Path, reviews, pulse, note) -> list[str]:
    failures = check_directory(_write(tmp_path, reviews, pulse, note))
    return [failure.rule for failure in failures]


def test_passing_pulse_clears_every_check(tmp_path: Path):
    quotes = _quotes()
    note = _note(quotes)
    assert len(note.split()) <= 280
    assert _rules(tmp_path, _reviews(), _pulse(quotes=quotes), note) == []


def test_theme_cap_rejects_six_theme_catalog(tmp_path: Path):
    quotes = _quotes()
    pulse = _pulse(themes=THEMES + EXTRA_THEMES, quotes=quotes)
    assert len(pulse.themes) == 6
    assert _rules(tmp_path, _reviews(), pulse, _note(quotes)) == ["theme_cap"]


def test_theme_cap_rejects_a_fourth_theme_in_the_note(tmp_path: Path):
    quotes = _quotes()
    pulse = _pulse(themes=THEMES + (STATEMENTS,), quotes=quotes)
    note = _note(quotes, extra="Statements still need a clearer download path for users.")
    assert _rules(tmp_path, _reviews(), pulse, note) == ["theme_cap"]


@pytest.mark.parametrize("size", [2, 4])
def test_quote_count_rejects_anything_but_three(tmp_path: Path, size: int):
    quotes = _quotes()
    if size == 2:
        chosen = quotes[:2]
    else:
        chosen = quotes + (
            Quote("asked for the same documents", 2, "play", "kyc", "rev-kyc"),
        )
    assert len(chosen) == size
    assert _rules(tmp_path, _reviews(), _pulse(quotes=chosen), _note(chosen)) == [
        "quote_count"
    ]


def test_verbatim_rejects_a_one_word_change(tmp_path: Path):
    quotes = _quotes()
    changed = (
        replace(quotes[0], text="KYC verification asked for the same papers three times"),
        quotes[1],
        quotes[2],
    )
    assert _rules(tmp_path, _reviews(), _pulse(quotes=changed), _note(changed)) == [
        "verbatim"
    ]


@pytest.mark.parametrize("theme_id", ["unclassified", "statements"])
def test_theme_match_rejects_quotes_outside_the_top_three(
    tmp_path: Path, theme_id: str
):
    reviews = _reviews()
    reviews[0] = replace(reviews[0], theme_id=theme_id)
    quotes = _quotes()
    assert _rules(tmp_path, reviews, _pulse(quotes=quotes), _note(quotes)) == [
        "theme_match"
    ]


def test_action_count_rejects_an_action_that_omits_its_theme(tmp_path: Path):
    actions = (
        replace(
            ACTIONS[0],
            text="Fix the document loop so users are not asked to upload the same file again.",
        ),
        ACTIONS[1],
        ACTIONS[2],
    )
    quotes = _quotes()
    assert _rules(tmp_path, _reviews(), _pulse(quotes=quotes, actions=actions), _note(quotes)) == [
        "action_count"
    ]


def test_length_rejects_a_note_over_280_words(tmp_path: Path):
    quotes = _quotes()
    note = _note(quotes, extra=" ".join(["padding"] * 300))
    assert len(note.split()) > 280
    assert _rules(tmp_path, _reviews(), _pulse(quotes=quotes), note) == ["length"]


def test_privacy_rejects_an_email_in_the_note(tmp_path: Path):
    quotes = _quotes()
    note = _note(quotes, extra="Reach user@example.com if this summary needs a correction.")
    assert _rules(tmp_path, _reviews(), _pulse(quotes=quotes), note) == ["privacy"]


def test_privacy_rejects_a_personal_name_in_a_quote(tmp_path: Path):
    reviews = _reviews()
    named = "Employees like Ansh Agarwal constantly mute and disconnect calls"
    reviews[0] = replace(reviews[0], text=named)
    quotes = _quotes()
    copied = (
        replace(quotes[0], text="Employees like Ansh Agarwal constantly mute"),
        quotes[1],
        quotes[2],
    )
    assert _rules(tmp_path, reviews, _pulse(quotes=copied), _note(copied)) == ["privacy"]


def test_privacy_rejects_a_redacted_token_in_a_quote(tmp_path: Path):
    reviews = _reviews()
    body = "I uploaded proof and the reply said [redacted] after two days of waiting"
    reviews[0] = replace(reviews[0], text=body)
    quotes = _quotes()
    copied = (
        replace(quotes[0], text="the reply said [redacted] after two days"),
        quotes[1],
        quotes[2],
    )
    assert _rules(tmp_path, reviews, _pulse(quotes=copied), _note(copied)) == ["privacy"]


def test_window_rejects_a_cited_review_before_the_window(tmp_path: Path):
    reviews = _reviews()
    reviews[0] = replace(reviews[0], created_at="2026-01-15")
    quotes = _quotes()
    assert _rules(tmp_path, reviews, _pulse(quotes=quotes), _note(quotes)) == ["window"]
