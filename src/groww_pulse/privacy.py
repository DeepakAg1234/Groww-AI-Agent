"""Drop reviewer identity and redact contact details inside review text."""

from __future__ import annotations

import re

from groww_pulse.models import Review

REDACTED = "[redacted]"
# Keep a review only when the redacted body has more than this many words.
MIN_REVIEW_WORDS = 8
# Two whole-word hits is enough to treat romanized Hindi as not English.
_HINDI_MARKER_HITS = 2

# Romanized Hindi words that rarely appear as whole English words.
_HINDI_MARKERS = frozenset(
    """
    aaj aap aapka aapke aapko aapne abhi acha achcha achha accha aur bahot
    bahut bakwas bas bekar bhai bhi bhot bohot bohot chahiye diya gatiya gaya
    gaye gayi ghatiya hai hain hi ho hota hote hoti hojata hun hoon hu inka
    inke inki iska iske iski isliye jaldi jada jyada ka kar kare karna karta
    karte karti karke ke ki kiya ko kya kyunki lekin liya mat mein mera mere
    meri mujhe nahi nahin nhi paise paisa par pe pesa raha rahe rahi rahta rha
    rhe sab se tha thi thik theek unka unke unki uska uske uski wala wale wali
    yah ye yeh zyada
    """.split()
)

_EMOJI = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002600-\U000026FF"
    "\U00002700-\U000027BF"
    "\U00002B00-\U00002BFF"
    "\U0001F3FB-\U0001F3FF"
    "\u200d\ufe0f\ufe0e"
    "]+",
    flags=re.UNICODE,
)
_WORD = re.compile(r"[A-Za-z]+")
_SPACE = re.compile(r"\s+")

_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE = re.compile(r"(?:\+\d{1,3}[\s.-]?)?(?:\d[\s.-]?){9,14}\d")
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_DEVICE = re.compile(r"\b[0-9a-fA-F]{16,}\b")
_HANDLE = re.compile(r"(?<![A-Za-z0-9.])@[A-Za-z][A-Za-z0-9_]{1,30}\b")
# Two title-case words, the shape of a person named in a review body.
_PERSON_NAME = re.compile(r"\b[A-Z][a-z]{2,} [A-Z][a-z]{2,}\b")
# Product and competitor phrases that share that shape and are not people.
_NAME_ALLOWLIST = frozenset(
    {
        "angel one",
        "google pay",
        "play store",
        "mutual fund",
        "mutual funds",
        "bank nifty",
    }
)


def redact_text(value: str) -> str:
    """Replace emails, phones, device ids, and @handles with [redacted]."""
    value = _EMAIL.sub(REDACTED, value)
    value = _UUID.sub(REDACTED, value)
    value = _DEVICE.sub(REDACTED, value)
    value = _PHONE.sub(REDACTED, value)
    value = _HANDLE.sub(REDACTED, value)
    return value


def contains_pii(value: str) -> bool:
    """True when text still contains an email, phone, device id, or @handle."""
    return redact_text(value) != value


def personal_names(value: str) -> tuple[str, ...]:
    """Return title-case names such as ``Ansh Agarwal``.

    Product phrases in the allowlist, such as ``Angel One``, are ignored.
    """
    found: list[str] = []
    seen: set[str] = set()
    for match in _PERSON_NAME.finditer(value):
        phrase = match.group(0)
        key = phrase.casefold()
        if key in _NAME_ALLOWLIST or key in seen:
            continue
        seen.add(key)
        found.append(phrase)
    return tuple(found)


def strip_emoji(value: str) -> str:
    """Remove emoji characters and collapse the spaces they leave behind."""
    return _SPACE.sub(" ", _EMOJI.sub(" ", value)).strip()


def is_english_text(value: str) -> bool:
    """True when the body is English rather than another language.

    Native scripts are detected from non-Latin letters. Romanized Hindi is
    detected from whole-word markers such as ``nahi`` and ``bahut``.
    """
    letters = [char for char in value if char.isalpha()]
    if not letters:
        return False
    non_latin = sum(1 for char in letters if ord(char) > 127)
    if non_latin >= 2:
        return False
    tokens = [word.lower() for word in _WORD.findall(value)]
    markers = sum(1 for word in tokens if word in _HINDI_MARKERS)
    return markers < _HINDI_MARKER_HITS


def apply_privacy(reviews: list[Review]) -> tuple[list[Review], int]:
    """Redact title and text.

    Drop a review whose body is empty afterwards, has 8 words or fewer,
    is only emoji once those characters are removed, or is not English.
    Emoji inside an English review are removed and the review is kept
    when the remaining words still clear the length bar.
    """
    kept: list[Review] = []
    dropped = 0
    for review in reviews:
        title = strip_emoji(redact_text(review.title)).strip()
        text = strip_emoji(redact_text(review.text)).strip()
        if len(text.split()) <= MIN_REVIEW_WORDS or not is_english_text(text):
            dropped += 1
            continue
        kept.append(
            Review(
                id=review.id,
                store=review.store,
                app_id=review.app_id,
                rating=review.rating,
                title=title,
                text=text,
                created_at=review.created_at,
                theme_id=review.theme_id,
            )
        )
    return kept, dropped
