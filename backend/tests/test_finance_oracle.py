"""Differential tests: TORA's finance engine against pyxirr, an independent implementation.

Two implementations disagreeing is the cheapest bug detector available — these run over a grid
of realistic Indian loan and SIP shapes, so a regression in the engines has to survive both.
"""
import pytest

pyxirr = pytest.importorskip("pyxirr", reason="oracle library not installed")

from backend.finance.engine import emi, required_sip, sip_future_value

LOANS = [(2000000, 8.5, 240), (3000000, 8.4, 300), (500000, 10.0, 36), (155000, 42.0, 24),
         (25000000, 9.25, 360), (100000, 12.5, 12)]
SIPS = [(10000, 12.0, 10), (5000, 10.0, 5), (25000, 11.0, 20), (2000, 8.0, 3)]   # years


@pytest.mark.parametrize("principal, rate, months", LOANS)
def test_emi_matches_the_oracle(principal, rate, months):
    ours = emi(principal, rate, months)
    theirs = pyxirr.pmt(rate / 1200, months, -principal)
    # the engine reports to the paisa, so the oracle is compared at that precision
    assert ours["emi"] == pytest.approx(theirs, abs=0.01)
    assert ours["total_payment"] == pytest.approx(theirs * months, abs=0.01)
    assert ours["total_interest"] == pytest.approx(theirs * months - principal, abs=0.01)


@pytest.mark.parametrize("monthly, rate, years", SIPS)
def test_sip_growth_matches_the_oracle(monthly, rate, years):
    """TORA models a SIP as an annuity-due: the instalment goes in on the SIP date and earns
    for that month, which is how a real SIP behaves — so the oracle is told the same."""
    ours = sip_future_value(monthly, rate, years)
    theirs = pyxirr.fv(rate / 1200, int(years * 12), -monthly, 0, pmt_at_beginning=True)
    assert ours["future_value"] == pytest.approx(theirs, abs=0.01)
    # and the ordinary-annuity reading (instalment at month end) is measurably smaller
    assert pyxirr.fv(rate / 1200, int(years * 12), -monthly, 0) < ours["future_value"]


@pytest.mark.parametrize("target, rate, years", [(10000000, 12.0, 20), (2500000, 10.0, 10),
                                                 (500000, 8.0, 3)])
def test_required_sip_matches_the_oracle(target, rate, years):
    ours = required_sip(target, rate, years)
    theirs = -pyxirr.pmt(rate / 1200, int(years * 12), 0, target, pmt_at_beginning=True)
    assert ours["monthly_sip"] == pytest.approx(theirs, abs=0.01)


def test_a_zero_rate_loan_is_just_the_principal_split():
    ours = emi(120000, 0, 12)
    assert ours["emi"] == pytest.approx(10000)
    assert ours["total_interest"] == pytest.approx(0)
