

def test_prepay_comparison_rows_match_the_options():
    """The answer copies these rows verbatim, so each value must come from an option."""
    from backend.finance.advisor import prepay_vs_invest

    out = prepay_vs_invest(loan_balance=250000, loan_rate=15, remaining_months=60, amount=5000)
    rows = {r["item"]: r for r in out["comparison"]}
    by_option = {o["option"]: o for o in out["options"]}
    money = lambda text: float(text.replace("₹", "").replace(",", ""))
    assert list(rows) == ["Net worth at the loan's original end", "Loan interest paid", "Loan closes in"]
    for name, option in by_option.items():
        assert money(rows["Net worth at the loan's original end"][name]) == round(option["net_worth_at_loan_end"])
        assert money(rows["Loan interest paid"][name]) == round(option["loan_interest_paid"])
        assert rows["Loan closes in"][name] == f"{option['loan_closes_in_months']} months"
    # prepaying clears the loan sooner and costs less interest than investing the same money
    assert money(rows["Loan interest paid"]["prepay"]) < money(rows["Loan interest paid"]["invest"])


def test_prepay_comparison_keeps_the_investments_row_when_it_differs():
    from backend.finance.advisor import prepay_vs_invest

    out = prepay_vs_invest(loan_balance=2000000, loan_rate=8.5, remaining_months=240, amount=500000,
                           amount_type="lump_sum")
    rows = [r["item"] for r in out["comparison"]]
    options = {o["option"]: o for o in out["options"]}
    differs = any(o["investment_value_after_tax"] != o["net_worth_at_loan_end"] for o in options.values())
    assert ("Investments after tax" in rows) == differs
