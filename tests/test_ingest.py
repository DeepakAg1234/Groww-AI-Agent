import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from groww_pulse.checks import load_reviews_jsonl
from groww_pulse.ingest.file_export import load_reviews
from groww_pulse.models import Review
from groww_pulse.pipeline import write_clean_reviews
from groww_pulse.normalize import review_id
from groww_pulse.privacy import apply_privacy, redact_text
from groww_pulse.weeks import week_window

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "play-sample.csv"
APP_ID = "com.nextbillion.groww"
IDENTITY = (
    "Asha Reviewer",
    "profile-asha-should-drop",
    "user-asha-should-drop",
    "device-asha-should-drop",
    "user-zara",
    "aaaaaaaaaaaaaaaa",
)


def _load_fixture():
    start, end = week_window("2026-W41", 10)
    return load_reviews(
        FIXTURE, app_id=APP_ID, window_start=start, window_end=end
    )


def test_week_window_for_default_config():
    start, end = week_window("2026-W41", 10)
    assert start.isoformat() == "2026-08-03"
    assert end.isoformat() == "2026-10-11"


def test_fixture_keeps_redacted_deduped_reviews():
    result = _load_fixture()
    assert result.rows_read == 7
    assert result.rows_kept == 2
    assert result.rows_dropped == 5
    assert result.rows_read == result.rows_kept + result.rows_dropped

    texts = [review.text for review in result.reviews]
    kept_text = "Smooth onboarding and fast KYC completed without extra documents"
    assert texts.count(kept_text) == 1
    kept = {review.text: review for review in result.reviews}
    valid = kept[kept_text]
    assert valid.title == "First title"
    assert valid.store == "play"
    assert valid.app_id == APP_ID
    assert valid.rating == 5
    assert valid.created_at == "2026-09-15"
    assert valid.theme_id == ""

    redacted = kept[
        "Email me at [redacted] or call [redacted] about the withdrawal"
    ]
    assert "user@example.com" not in redacted.text
    assert "9876543210" not in redacted.text
    assert redacted.created_at == "2026-10-01"
    assert redacted.rating == 2

    for review in result.reviews:
        assert week_window("2026-W41", 10)[0].isoformat() <= review.created_at
        assert review.created_at <= week_window("2026-W41", 10)[1].isoformat()

    blob = " ".join(
        f"{review.id} {review.title} {review.text} {review.app_id}"
        for review in result.reviews
    )
    for secret in IDENTITY:
        assert secret not in blob
    field_names = {field.name for field in dataclasses.fields(Review)}
    assert field_names.isdisjoint(
        {"user_name", "reviewer", "profile_link", "user_id", "device_id"}
    )


def test_second_run_produces_the_same_ids():
    first = _load_fixture()
    second = _load_fixture()
    assert [review.id for review in first.reviews] == [
        review.id for review in second.reviews
    ]
    body = "Smooth onboarding and fast KYC completed without extra documents"
    expected = hashlib.sha256(
        f"com.nextbillion.groww|2026-09-15|{body}".encode()
    ).hexdigest()
    assert review_id(APP_ID, "2026-09-15", body) == expected
    valid = next(review for review in first.reviews if review.text == body)
    assert valid.id == expected


def test_header_aliases_and_default_app_id(tmp_path: Path):
    path = tmp_path / "alias.csv"
    path.write_text(
        "score,body,updated,summary,user_name\n"
        "4,Alias body stays visible after the header mapping change,2026-09-01,Alias title,Hidden Name\n",
        encoding="utf-8",
    )
    start, end = week_window("2026-W41", 10)
    result = load_reviews(path, app_id=APP_ID, window_start=start, window_end=end)
    assert result.rows_kept == 1
    review = result.reviews[0]
    assert review.text == "Alias body stays visible after the header mapping change"
    assert review.title == "Alias title"
    assert review.rating == 4
    assert review.created_at == "2026-09-01"
    assert review.app_id == APP_ID
    assert review.store == "play"
    assert "Hidden Name" not in review.text


def test_package_column_overrides_default_app_id(tmp_path: Path):
    path = tmp_path / "packages.csv"
    path.write_text(
        "rating,text,date,package\n"
        "5,From the stocks app the order status loaded without delay,2026-09-10,com.nextbillion.groww.stocks\n",
        encoding="utf-8",
    )
    start, end = week_window("2026-W41", 10)
    result = load_reviews(path, app_id=APP_ID, window_start=start, window_end=end)
    assert result.reviews[0].app_id == "com.nextbillion.groww.stocks"


def test_json_export(tmp_path: Path):
    path = tmp_path / "reviews.json"
    path.write_text(
        json.dumps(
            {
                "reviews": [
                    {
                        "stars": 3,
                        "content": "Ping @groww_help about KYC taking too long this week",
                        "at": "2026-10-02T18:30:00Z",
                        "title": "KYC",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    start, end = week_window("2026-W41", 10)
    result = load_reviews(path, app_id=APP_ID, window_start=start, window_end=end)
    assert result.rows_kept == 1
    review = result.reviews[0]
    assert review.created_at == "2026-10-02"
    assert review.text == "Ping [redacted] about KYC taking too long this week"
    assert review.store == "play"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "mail a@b.co call +91 98765 43210",
            "mail [redacted] call [redacted]",
        ),
        (
            "device 123e4567-e89b-12d3-a456-426614174000",
            "device [redacted]",
        ),
        (
            "android a1b2c3d4e5f67890 seen",
            "android [redacted] seen",
        ),
        ("posted on 2026-09-15", "posted on 2026-09-15"),
    ],
)
def test_redaction_patterns(raw: str, expected: str):
    assert redact_text(raw) == expected


def test_privacy_drops_reviews_of_eight_words_or_fewer():
    short = Review(
        id="short",
        store="play",
        app_id=APP_ID,
        rating=4,
        title="Brief",
        text="one two three four five six seven eight",
        created_at="2026-09-15",
    )
    long = Review(
        id="long",
        store="play",
        app_id=APP_ID,
        rating=4,
        title="Enough",
        text="one two three four five six seven eight nine",
        created_at="2026-09-15",
    )
    kept, dropped = apply_privacy([short, long])
    assert dropped == 1
    assert [review.id for review in kept] == ["long"]


def test_privacy_drops_emoji_only_and_non_english_reviews():
    emoji_only = Review(
        id="emoji",
        store="play",
        app_id=APP_ID,
        rating=5,
        title="",
        text="😀 😀 😀 😀 😀 😀 😀 😀 😀",
        created_at="2026-09-15",
    )
    padded = Review(
        id="padded",
        store="play",
        app_id=APP_ID,
        rating=5,
        title="Nice",
        text="This app is very good for me today 💯",
        created_at="2026-09-15",
    )
    english = Review(
        id="english",
        store="play",
        app_id=APP_ID,
        rating=4,
        title="Stars",
        text="The withdrawal screen needs a clearer message for users today 😊",
        created_at="2026-09-15",
    )
    hindi = Review(
        id="hindi",
        store="play",
        app_id=APP_ID,
        rating=1,
        title="",
        text="बहुत ही घटिया सर्विस है इनका आज एक महीने से फोन करके",
        created_at="2026-09-15",
    )
    hinglish = Review(
        id="hinglish",
        store="play",
        app_id=APP_ID,
        rating=2,
        title="",
        text="mujhe bonus chahiye jaldi isliye paanch star de raha hun abhi",
        created_at="2026-09-15",
    )
    kept, dropped = apply_privacy([emoji_only, padded, english, hindi, hinglish])
    assert dropped == 4
    assert [review.id for review in kept] == ["english"]
    assert "😊" not in kept[0].text
    assert kept[0].text == (
        "The withdrawal screen needs a clearer message for users today"
    )


def test_write_clean_reviews_writes_the_filtered_handoff(tmp_path: Path):
    export = tmp_path / "play.csv"
    export.write_bytes(FIXTURE.read_bytes())
    config = tmp_path / "config.yaml"
    config.write_text(
        "\n".join(
            [
                "product_name: Groww",
                "window_weeks: 10",
                "apps:",
                "  - store: play",
                f"    app_id: {APP_ID}",
                "    export: play.csv",
                'email_to: ""',
                'doc_title_pattern: "Groww weekly pulse — {week}"',
                "max_themes: 5",
                "pulse_theme_count: 3",
                "quote_count: 3",
                "action_count: 3",
                "target_words: 250",
                "max_words: 280",
                "",
            ]
        ),
        encoding="utf-8",
    )
    destination = write_clean_reviews(
        config, "2026-W41", runs_dir=tmp_path / "runs"
    )
    again = write_clean_reviews(config, "2026-W41", runs_dir=tmp_path / "runs")
    reviews = load_reviews_jsonl(destination)
    assert again == destination
    assert destination == tmp_path / "runs" / "2026-W41" / "reviews.jsonl"
    assert len(reviews) == 2
    assert all(review.theme_id == "" for review in reviews)
    assert all(review.store == "play" for review in reviews)


def test_privacy_drops_blank_body():
    review = Review(
        id="x",
        store="play",
        app_id=APP_ID,
        rating=5,
        title="Hi",
        text="   ",
        created_at="2026-09-15",
    )
    kept, dropped = apply_privacy([review])
    assert kept == []
    assert dropped == 1
