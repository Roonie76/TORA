"""TORA deterministic income-tax tool (Phase 4C)."""

from typing import Any, Dict, Literal, Type

from pydantic import BaseModel, Field

from .base import BaseTool, ToolMetadata
from ..finance.engine import FinanceInputError
from ..finance.tax import TAX_OPERATIONS, TAX_PARAMS, TAX_RULES, current_tax_year
from ..finance.tax_extras import EXTRA_TAX_OPERATIONS, EXTRA_TAX_PARAMS

_ALLOWED = {"gross_salary", "other_income", "regime", "tax_year", "age_category", "deductions",
            "stcg_equity", "ltcg_equity", "ltcg_other", "hra_exempt", "house_property_income"}
_ALL_PARAMS_DOC = "compute_tax/compare_regimes(" + TAX_PARAMS + "); " + "; ".join(
    f"{op}({args})" for op, args in EXTRA_TAX_PARAMS.items())


_DEDUCTION_KEYS = {"section_80c", "section_80d_self", "section_80d_parents", "section_80ccd_1b",
                   "home_loan_interest", "savings_interest", "employer_nps"}
# Common spellings a model (or person) uses for the same deductions.
_DEDUCTION_ALIASES = {
    "80c": "section_80c", "section80c": "section_80c", "sec_80c": "section_80c",
    "80d": "section_80d_self", "section_80d": "section_80d_self", "section80d": "section_80d_self",
    "80d_self": "section_80d_self", "80d_parents": "section_80d_parents",
    "80ccd_1b": "section_80ccd_1b", "80ccd1b": "section_80ccd_1b", "section_80ccd": "section_80ccd_1b", "nps": "section_80ccd_1b",
    "home_loan": "home_loan_interest", "housing_loan_interest": "home_loan_interest", "section_24": "home_loan_interest",
    "section_24b": "home_loan_interest", "interest_on_home_loan": "home_loan_interest",
    "80tta": "savings_interest", "80ttb": "savings_interest",
    "salary": "gross_salary", "annual_salary": "gross_salary",
}


def _canonical(key: str) -> str:
    k = str(key).strip().lower().replace(" ", "_").replace("-", "_")
    return _DEDUCTION_ALIASES.get(k, k)


def normalise_tax_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Fold deduction amounts given at the top level (or under other names) into `deductions`."""
    clean: Dict[str, Any] = {}
    deductions: Dict[str, Any] = {}
    raw_deductions = params.get("deductions")
    if isinstance(raw_deductions, dict):
        for k, v in raw_deductions.items():
            deductions[_canonical(k)] = v
    elif isinstance(raw_deductions, list):  # [{"section": "80c", "amount": 150000}]
        for item in raw_deductions:
            if isinstance(item, dict):
                name = item.get("section") or item.get("name")
                if name is not None and "amount" in item:
                    deductions[_canonical(name)] = item["amount"]
    for key, value in params.items():
        if key == "deductions":
            continue
        canon = _canonical(key)
        if canon in _DEDUCTION_KEYS:
            deductions[canon] = value
        elif canon == "gross_salary" and "gross_salary" in params and key != "gross_salary":
            continue
        else:
            clean[canon] = value
    if deductions:
        clean["deductions"] = deductions
    return clean


def _legal_basis(operation: str, params: Dict[str, Any]) -> list:
    """Citations for the rules this calculation relied on (from the reviewed rules library)."""
    from ..knowledge import get_library

    ty = params.get("tax_year")
    ids = ["new-regime-slabs", "rebate", "surcharge-cess"]
    if operation == "compare_regimes" or params.get("regime") == "old":
        ids.insert(1, "old-regime-slabs")
    if params.get("gross_salary"):
        ids.append("standard-deduction")
    deductions = params.get("deductions") or {}
    for key, rid in (("section_80c", "80c"), ("section_80d_self", "80d"), ("section_80d_parents", "80d"),
                     ("section_80ccd_1b", "80ccd"), ("employer_nps", "80ccd"), ("home_loan_interest", "home-loan-interest"),
                     ("savings_interest", "savings-interest")):
        if key in deductions and rid not in ids:
            ids.append(rid)
    if params.get("stcg_equity"):
        ids.append("stcg-equity")
    if params.get("ltcg_equity"):
        ids.append("ltcg-equity")
    if params.get("ltcg_other"):
        ids.append("ltcg-other")
    lib = get_library()
    out = []
    for rid in ids:
        rule = lib.get(rid)
        if rule is not None and rule.applies_to(ty):
            out.append(f"{rule.title}: {rule.citation_for(ty)}")
    return out


class TaxCalcInput(BaseModel):
    operation: Literal["compute_tax", "compare_regimes", "hra_exemption", "house_property_income",
                       "capital_gains_tax", "advance_tax_plan", "itr_form_choice", "tax_saving_finder"] = Field(
        ..., description="compute_tax for one regime, compare_regimes for new vs old, or a specialised calculation."
    )
    params: Dict[str, Any] = Field(default_factory=dict, description="Annual amounts in rupees.")


class TaxCalcTool(BaseTool):
    name: str = "tax_calc"
    description: str = (
        "Deterministic Indian income-tax calculator for resident individuals (tax years "
        + ", ".join(sorted(TAX_RULES))
        + "): slab tax, standard deduction, rebate with marginal relief, surcharge, cess, capital gains, "
        "new-vs-old regime comparison, HRA exemption, house-property income, tax on one asset sale, advance-tax "
        "instalments, which ITR form to file, and a finder for unused deductions. Amounts are ANNUAL rupees "
        "unless the parameter says monthly. Operations and params: " + _ALL_PARAMS_DOC
    )
    args_schema: Type[BaseModel] = TaxCalcInput

    def __init__(self):
        self.metadata = ToolMetadata(
            name=self.name, description=self.description, version="1.0.0",
            tags=["finance", "tax", "deterministic"], is_deterministic=True, requires_auth=False,
        )
        super().__init__()

    async def execute(self, operation: str, params: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        if operation in EXTRA_TAX_OPERATIONS:
            import inspect

            fn = EXTRA_TAX_OPERATIONS[operation]
            sig = inspect.signature(fn)
            unknown = set(params) - set(sig.parameters)
            if unknown:
                raise FinanceInputError(f"Unknown parameter(s) for {operation}: {', '.join(sorted(unknown))}. "
                                        f"Expected: {EXTRA_TAX_PARAMS[operation]}.")
            missing = [n for n, p in sig.parameters.items() if p.default is inspect._empty and n not in params]
            if missing:
                raise FinanceInputError(f"Missing parameter(s) for {operation}: {', '.join(missing)}.")
            return fn(**params)
        params = normalise_tax_params(params)
        unknown = set(params) - _ALLOWED
        if unknown:
            raise FinanceInputError(f"Unknown parameter(s): {', '.join(sorted(unknown))}. Expected: {TAX_PARAMS}.")
        clean = dict(params)
        if operation == "compare_regimes":
            clean.pop("regime", None)
        clean.setdefault("tax_year", current_tax_year())
        result = TAX_OPERATIONS[operation](**clean)
        result["legal_basis"] = _legal_basis(operation, clean)
        return result
