"""Indian money formatting: CLDR (Babel) with a hand-rolled fallback that must agree."""
import importlib

import pytest

from backend.finance import engine
from backend.finance.engine import _group_indian, inr

CASES = [(0, "₹0"), (5, "₹5"), (999, "₹999"), (1000, "₹1,000"),
         (23954.98, "₹23,955"), (99999, "₹99,999"), (100000, "₹1,00,000"),
         (370000, "₹3,70,000"), (1234567.8, "₹12,34,568"), (12345678.9, "₹1,23,45,679"),
         (100000000, "₹10,00,00,000"), (-45500.5, "-₹45,500")]


@pytest.mark.parametrize("value, expected", CASES)
def test_indian_grouping(value, expected):
    assert inr(value) == expected


@pytest.mark.parametrize("value, expected", CASES)
def test_fallback_matches_babel(value, expected, monkeypatch):
    """A deployment without Babel must format money identically."""
    monkeypatch.setattr(engine, "_format_decimal", None)
    assert inr(value) == expected
    assert _group_indian(int(round(abs(value)))) == expected.lstrip("-₹")


def test_babel_is_the_one_actually_used():
    assert engine._format_decimal is not None, "Babel missing: install it or the fallback silently takes over"
    assert importlib.import_module("babel.numbers").format_decimal(370000, format="#,##,##0",
                                                                   locale="en_IN") == "3,70,000"
