"""
Numeric grounding verifier (Phase 4B).

After the model writes an answer, every rupee amount (and, for research answers,
every percentage) is checked against evidence TORA actually has: the user's own
words, the stored financial profile, tool outputs (calculators, tax engine,
research), earlier turns and stored research. Figures explicitly labelled as
examples/assumptions are allowed. Unsupported figures trigger one corrective
regeneration and, failing that, a visible caveat.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

_UNITS = {
    "k": 1_000.0, "thousand": 1_000.0,
    "l": 100_000.0, "lac": 100_000.0, "lacs": 100_000.0, "lakh": 100_000.0, "lakhs": 100_000.0,
    "cr": 10_000_000.0, "crore": 10_000_000.0, "crores": 10_000_000.0,
    "mn": 1_000_000.0, "million": 1_000_000.0,
}
_UNIT_ALT = r"(?:lakhs|lakh|lacs|lac|crores|crore|cr|thousand|million|mn|k|l)"
_NUM = r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_MONEY_RE = re.compile(
    rf"(?:₹|\brs\.?|\binr)\s*(?P<num>{_NUM})(?:\s*(?P<unit>{_UNIT_ALT})\b)?"
    rf"|(?P<num2>{_NUM})\s*(?P<unit2>lakhs|lakh|lacs|lac|crores|crore|cr)\b",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(rf"(?P<num>\d+(?:\.\d+)?)\s*(?:%|percent\b|per\s*cent\b)", re.IGNORECASE)
_ANY_NUMBER_RE = re.compile(rf"(?P<num>{_NUM})(?:\s*(?P<unit>{_UNIT_ALT})\b)?", re.IGNORECASE)
_LABELLED = re.compile(
    r"(?<!\w)(?:for example|for instance|e\.g\.|example|illustrat\w*|hypothetical\w*|assum\w*|suppose|say you|"
    r"let'?s say|if you|if your|such as|scenario)(?!\w)",
    re.IGNORECASE,
)
# Sentence boundaries, but not after abbreviations common in Indian finance text
_SENTENCE_SPLIT = re.compile(
    r"(?<![Rr]s\.)(?<!e\.g\.)(?<!i\.e\.)(?<!p\.a\.)(?<!\bvs\.)(?<!\bNo\.)(?<!approx\.)(?<=[.!?])\s+|\n+"
)

MIN_MONEY = 100.0
MAX_DERIVED_BASE = 30


def _to_float(num: str) -> float:
    return float(num.replace(",", ""))


def _decimals(num: str) -> int:
    return len(num.split(".")[1]) if "." in num else 0


@dataclass
class NumericClaim:
    text: str
    value: float
    kind: str  # "money" | "percent"
    precision: float
    sentence: str
    labelled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "value": self.value, "kind": self.kind, "labelled": self.labelled}


@dataclass
class GroundingReport:
    checked: int = 0
    supported: List[NumericClaim] = field(default_factory=list)
    unsupported: List[NumericClaim] = field(default_factory=list)
    labelled: List[NumericClaim] = field(default_factory=list)
    soft_unsupported: List[NumericClaim] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unsupported

    def to_dict(self) -> Dict[str, Any]:
        return {
            "checked": self.checked,
            "ok": self.ok,
            "unsupported": [c.to_dict() for c in self.unsupported],
            "labelled_assumptions": [c.to_dict() for c in self.labelled],
            "soft_unsupported": [c.to_dict() for c in self.soft_unsupported],
        }


def extract_claims(text: str) -> List[NumericClaim]:
    claims: List[NumericClaim] = []
    for sentence in _SENTENCE_SPLIT.split(text or ""):
        if not sentence.strip():
            continue
        labelled = bool(_LABELLED.search(sentence))
        taken = []
        for m in _MONEY_RE.finditer(sentence):
            num = m.group("num") or m.group("num2")
            unit = (m.group("unit") or m.group("unit2") or "").lower()
            mult = _UNITS.get(unit, 1.0)
            value = _to_float(num) * mult
            taken.append(m.span())
            if value < MIN_MONEY:
                continue
            # Precision implied by how the figure is written (₹21.66 lakh -> ±500), capped at 3%
            precision = min(0.5 * (10 ** -_decimals(num)) * mult, max(0.5, 0.03 * value))
            claims.append(NumericClaim(m.group(0).strip(), value, "money", precision, sentence.strip(), labelled))
        for m in _PERCENT_RE.finditer(sentence):
            if any(a <= m.start() < b for a, b in taken):
                continue
            num = m.group("num")
            claims.append(NumericClaim(m.group(0).strip(), _to_float(num), "percent",
                                       0.5 * (10 ** -_decimals(num)), sentence.strip(), labelled))
    return claims


def numbers_in_text(text: str) -> List[float]:
    values: List[float] = []
    for m in _ANY_NUMBER_RE.finditer(text or ""):
        try:
            base = _to_float(m.group("num"))
        except ValueError:
            continue
        unit = (m.group("unit") or "").lower()
        values.append(base * _UNITS.get(unit, 1.0))
        if unit:
            values.append(base)
    return values


def _walk(obj: Any, out: List[float], depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.append(float(obj))
    elif isinstance(obj, str):
        out.extend(numbers_in_text(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk(v, out, depth + 1)
    elif isinstance(obj, (list, tuple, set)):
        for v in list(obj)[:500]:
            _walk(v, out, depth + 1)


class Evidence:
    def __init__(self) -> None:
        self.values: List[float] = []
        self._base: List[float] = []

    def add_text(self, text: Optional[str], derive: bool = False) -> None:
        nums = numbers_in_text(text or "")
        self.values.extend(nums)
        if derive:
            self._base.extend(v for v in nums if v >= MIN_MONEY)

    def add_object(self, obj: Any) -> None:
        _walk(obj, self.values)

    def add_profile(self, profile: Any) -> None:
        if profile is None or not hasattr(profile, "iter_current_facts"):
            return
        facts = list(profile.iter_current_facts()) + list(getattr(profile, "scenarios", []))
        for f in facts:
            vals = [f.value] + [r.value for r in getattr(f, "revisions", [])]
            for v in vals:
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    self.values.append(float(v))
                    if v >= MIN_MONEY and f.status == "current":
                        self._base.append(float(v))

    def finalize(self) -> None:
        """Add simple derived figures users routinely see: sums, differences, x12, /12, shares."""
        base = sorted(set(self._base), reverse=True)[:MAX_DERIVED_BASE]
        derived: List[float] = []
        for v in base:
            derived.extend((v * 12, v / 12))
        for a, b in itertools.combinations(base, 2):
            derived.extend((a + b, abs(a - b)))
            if a and b:
                derived.extend((b / a * 100, a / b * 100))
        if len(base) >= 3:
            derived.append(sum(base))
        self.values.extend(derived)

    def supports(self, claim: NumericClaim) -> bool:
        for v in self.values:
            tol = max(claim.precision, 0.001 * abs(v), 0.01)
            if abs(claim.value - v) <= tol:
                return True
        return False


def build_evidence(
    user_texts: Sequence[str] = (),
    profile: Any = None,
    tool_context: Any = None,
    other_texts: Sequence[str] = (),
) -> Evidence:
    ev = Evidence()
    for t in user_texts:
        ev.add_text(t, derive=True)
    for t in other_texts:
        ev.add_text(t)
    ev.add_profile(profile)
    if tool_context is not None and not tool_context.is_empty():
        for res in tool_context.results:
            if not res.is_error:
                ev.add_object(res.output)
                ev.add_object(res.metadata)
    ev.finalize()
    return ev


def verify_answer(answer: str, evidence: Evidence, strict_percentages: bool = False) -> GroundingReport:
    report = GroundingReport()
    for claim in extract_claims(answer):
        report.checked += 1
        if evidence.supports(claim):
            report.supported.append(claim)
        elif claim.labelled:
            report.labelled.append(claim)
        elif claim.kind == "percent" and not strict_percentages:
            report.soft_unsupported.append(claim)
        else:
            report.unsupported.append(claim)
    return report


def correction_instruction(report: GroundingReport) -> str:
    figures = ", ".join(dict.fromkeys(c.text for c in report.unsupported))
    return (
        "Grounding check: the following figures in your draft are not supported by the user's data, "
        f"the calculators or the research provided: {figures}. Rewrite the answer. Use only figures that "
        "appear in the provided data or tool results. If a figure is only an illustration or an assumption, "
        "say so explicitly (for example, 'assuming ...'). If required inputs are missing, ask the user for them."
    )


def caveat_note(report: GroundingReport) -> str:
    figures = ", ".join(dict.fromkeys(c.text for c in report.unsupported))
    return (
        f"\n\nNote: {figures} could not be verified against your data, TORA's calculators or the sources "
        "gathered — treat them as estimates."
    )
