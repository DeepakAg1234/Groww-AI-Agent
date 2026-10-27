"""Assign each review to one fixed theme with a local classifier.

The five product themes do not change from week to week. The first call
trains a TF-IDF vectorizer and a logistic regression model on
``theme_training.jsonl``. Later
calls reuse that model. Praise that names no product issue is
``general_appreciation``. A complaint or nonsense body that matches no
theme is ``unclassified``. Neither is one of the five product themes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import FeatureUnion, Pipeline

UNCLASSIFIED = "unclassified"
GENERAL_APPRECIATION = "general_appreciation"
_TRAINING_PATH = Path(__file__).with_name("theme_training.jsonl")
_APPRECIATION_WORD = re.compile(
    r"\b(?:nice|good|great|love|loved|loving|best|excellent|easy|awesome|"
    r"perfect|happy|superb|recommend|recommended|useful|smooth|thank|thanks|"
    r"beginners?|fine|trusted|amazing|wonderful|user[\s-]?friendly)\b",
    re.IGNORECASE,
)
_ISSUE_WORD = re.compile(
    r"\b(?:worst|useless|waste|scam|leak|leaking|fraud|court|crash|crashes|"
    r"crashed|lag|lags|delay|delayed|failed|failure|charge|charges|brokerage|"
    r"fee|fees|withdraw|withdrawal|support|ticket|kyc|login|bug|hang|hangs|"
    r"frozen|freeze|slow|order|orders|chart|charts|sip|upi|payment|payments|"
    r"mutual|fund|funds|payout)\b",
    re.IGNORECASE,
)
_NEGATIVE_WORD = re.compile(
    r"\b(?:bad|worst|useless|waste|poor|terrible|horrible|scam|fraud)\b",
    re.IGNORECASE,
)

_ORDERS = "orders_and_trading"
_CHARTS = "charts_speed_and_stability"
_SUPPORT = "customer_support"
_CHARGES = "charges_and_brokerage"
_PAYMENTS = "payments_withdrawals_and_mutual_funds"


@dataclass(frozen=True)
class CatalogTheme:
    id: str
    name: str
    definition: str
    examples: tuple[str, str]


FIXED_CATALOG: tuple[CatalogTheme, ...] = (
    CatalogTheme(
        id=_ORDERS,
        name="Orders and trading",
        definition="Placing, changing, or completing a stock, intraday, or IPO order.",
        examples=("order delay", "order failed"),
    ),
    CatalogTheme(
        id=_CHARTS,
        name="Charts, speed, and stability",
        definition="Charts, loading speed, crashes, and the app freezing.",
        examples=("chart lag", "slow chart"),
    ),
    CatalogTheme(
        id=_SUPPORT,
        name="Customer support",
        definition="Help requests, tickets, calls, KYC, and login problems that go unanswered.",
        examples=("customer support", "call centre"),
    ),
    CatalogTheme(
        id=_CHARGES,
        name="Charges and brokerage",
        definition="Brokerage, fees, and other charges on a trade.",
        examples=("brokerage fee", "high charges"),
    ),
    CatalogTheme(
        id=_PAYMENTS,
        name="Payments, withdrawals, and mutual funds",
        definition="Adding money, withdrawals, payouts, statements, and mutual fund orders.",
        examples=("withdrawal not credited", "mutual fund"),
    ),
)

# Curated examples, not a week's export. Smaller topics are folded in here:
# IPO with orders, KYC and login with support, statements with payments.
_EXAMPLES: tuple[tuple[str, str], ...] = (
    ("The order delay and the order failed this morning again today.", _ORDERS),
    ("Another order delay showed up when the order failed after lunch.", _ORDERS),
    ("order delay and order failed this morning", _ORDERS),
    ("order delay and order failed again", _ORDERS),
    ("My buy order stayed pending and was never executed.", _ORDERS),
    ("The sell order was rejected during market hours.", _ORDERS),
    ("Intraday trade was squared off automatically before the close.", _ORDERS),
    ("I could not place an order in the stock I wanted.", _ORDERS),
    ("The GTT order did not trigger at the price I set.", _ORDERS),
    ("Shares showed as purchased but the order status says failed.", _ORDERS),
    ("Unable to modify the order after it was sent to the exchange.", _ORDERS),
    ("The IPO bid I submitted was rejected and the order did not go through.", _ORDERS),
    ("My IPO application stayed pending and then failed.", _ORDERS),
    ("Limit order for the shares never reached the market.", _ORDERS),
    ("The chart lag and the slow chart made the open unusable today.", _CHARTS),
    ("A slow chart still loaded after the chart lag during the close.", _CHARTS),
    ("chart lag and slow chart at the open", _CHARTS),
    ("chart lag and slow chart after lunch", _CHARTS),
    ("The app crashes whenever I open the price chart.", _CHARTS),
    ("Charts take too long to load during market hours.", _CHARTS),
    ("The candlestick chart froze and stopped updating.", _CHARTS),
    ("The app is very slow and keeps freezing on the watchlist.", _CHARTS),
    ("The graph is blank and will not load at all.", _CHARTS),
    ("Price on the chart is delayed compared with the market.", _CHARTS),
    ("The app crashed three times while I was watching the chart.", _CHARTS),
    ("The app hangs and the screen goes blank after a few minutes.", _CHARTS),
    ("Customer support and the call centre never answered my request today.", _SUPPORT),
    ("The call centre and customer support left the ticket open all day.", _SUPPORT),
    ("customer support and the call centre never answered", _SUPPORT),
    ("No response from customer care after I raised a ticket.", _SUPPORT),
    ("The chat executive did not reply and the call was disconnected.", _SUPPORT),
    ("I opened a ticket and nobody from support called me back.", _SUPPORT),
    ("KYC is stuck and support will not help me finish it.", _SUPPORT),
    ("Login keeps failing and the helpdesk closed the chat.", _SUPPORT),
    ("My account is locked and no one from customer support replied.", _SUPPORT),
    ("The helpline disconnected and the complaint is still open.", _SUPPORT),
    ("Cannot log in and support has not answered the ticket.", _SUPPORT),
    ("The brokerage fee is high but this is still a nice app overall.", _CHARGES),
    ("high charges and the brokerage fee are too high", _CHARGES),
    ("Brokerage fee is much higher than the plan I chose.", _CHARGES),
    ("AMC charges were deducted without any notice.", _CHARGES),
    ("DP charges were added on every sell order.", _CHARGES),
    ("Hidden charges showed up on the contract note.", _CHARGES),
    ("They charged an extra fee on a very small trade.", _CHARGES),
    ("The brokerage plan changed and the fee went up.", _CHARGES),
    ("STT and other charges on the trade are unclear.", _CHARGES),
    ("Brokerage is higher than what was promised at signup.", _CHARGES),
    ("Too many charges and the brokerage keeps increasing.", _CHARGES),
    ("Withdrawal not credited to my bank account after several days.", _PAYMENTS),
    ("UPI payment failed but the money was still deducted.", _PAYMENTS),
    ("The mutual fund SIP did not get processed this month.", _PAYMENTS),
    ("Amount deducted from the demat account but not credited in the bank.", _PAYMENTS),
    ("The payout is pending and the withdrawal is still processing.", _PAYMENTS),
    ("I cannot add money to the wallet with UPI.", _PAYMENTS),
    ("The SIP instalment failed and the mutual fund was not bought.", _PAYMENTS),
    ("Fund transfer to my bank account failed again.", _PAYMENTS),
    ("The statement does not show the mutual fund units I bought.", _PAYMENTS),
    ("Redemption of the mutual fund is stuck and the payout has not come.", _PAYMENTS),
    ("Added funds but the balance never updated.", _PAYMENTS),
    ("nice app and easy to use overall", GENERAL_APPRECIATION),
    ("best app for beginners really", GENERAL_APPRECIATION),
    ("This is a nice app and easy to use overall for me.", GENERAL_APPRECIATION),
    ("Best app for beginners really and nothing more to add.", GENERAL_APPRECIATION),
    ("Really nice and simple, nothing else to say about it.", GENERAL_APPRECIATION),
    ("I like this application and I would recommend it to friends.", GENERAL_APPRECIATION),
    ("Very bad experience overall and I have no specific complaint.", UNCLASSIFIED),
    ("Good app, nothing else to mention in this review at all.", GENERAL_APPRECIATION),
    ("Worst app ever and I am done with it completely now.", UNCLASSIFIED),
    ("Please improve the app, that is all I wanted to say.", UNCLASSIFIED),
    ("Thank you, great app, highly recommended to everyone I know.", GENERAL_APPRECIATION),
    ("Smooth experience overall and I am happy with it so far.", GENERAL_APPRECIATION),
    ("Waste of time and a useless app with no detail to give.", UNCLASSIFIED),
    ("Excellent and super, five stars from me for the experience.", GENERAL_APPRECIATION),
    ("asdfgh jklqwerty zxcvbnm qwertyuiop nonsense letters only", UNCLASSIFIED),
    ("qwerty uiop asdf ghjkl zxcv bnmm keyboard mash body here", UNCLASSIFIED),
)


def fixed_catalog() -> tuple[CatalogTheme, ...]:
    """Return the five themes, in catalog order."""
    return FIXED_CATALOG


def is_general_appreciation(text: str) -> bool:
    """True when the review praises the app and names no product issue."""
    return (
        _APPRECIATION_WORD.search(text) is not None
        and _ISSUE_WORD.search(text) is None
    )


def classify_texts(texts: list[str] | tuple[str, ...]) -> list[str]:
    """Return one theme id, ``general_appreciation``, or ``unclassified``."""
    if not texts:
        return []
    model = _model()
    predicted = model.predict(list(texts))
    labels: list[str] = []
    for text, label in zip(texts, predicted, strict=True):
        chosen = str(label)
        if (
            chosen == GENERAL_APPRECIATION
            and _NEGATIVE_WORD.search(text)
            and _APPRECIATION_WORD.search(text) is None
        ):
            chosen = UNCLASSIFIED
        elif chosen == UNCLASSIFIED and is_general_appreciation(text):
            chosen = GENERAL_APPRECIATION
        labels.append(chosen)
    return labels


def _training_rows() -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in _TRAINING_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        rows.append((str(item["text"]), str(item["label"])))
    return rows


@lru_cache(maxsize=1)
def _model() -> Pipeline:
    rows = _training_rows()
    pipeline = Pipeline(
        [
            (
                "features",
                FeatureUnion(
                    [
                        (
                            "word",
                            TfidfVectorizer(
                                lowercase=True,
                                ngram_range=(1, 2),
                                min_df=1,
                                sublinear_tf=True,
                            ),
                        ),
                        (
                            "char",
                            TfidfVectorizer(
                                lowercase=True,
                                analyzer="char_wb",
                                ngram_range=(3, 5),
                                min_df=1,
                                sublinear_tf=True,
                            ),
                        ),
                    ]
                ),
            ),
            (
                "clf",
                LogisticRegression(
                    C=2.0,
                    max_iter=2000,
                    random_state=0,
                    solver="lbfgs",
                ),
            ),
        ]
    )
    pipeline.fit([text for text, _label in rows], [label for _text, label in rows])
    return pipeline
