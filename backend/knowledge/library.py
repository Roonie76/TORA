"""
Curated rules library (Phase 10).

Each rule is a short, plain-language statement of an official rule with its
figures, the tax years it covers, the exact legal citation per year, a source
link and the date it was last checked. TORA answers rule questions from here
(and cites the section) instead of from the model's memory or a web search.

Update workflow: edit rules.json -> `python -m backend.knowledge check`
(validates structure, flags stale entries, cross-checks figures against the
tax engine) -> have a CA review -> commit.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

RULES_PATH = Path(__file__).with_name("rules.json")
STALE_AFTER_DAYS = 365
_WORD = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?", re.IGNORECASE)
_STOP = {"the", "a", "an", "is", "of", "for", "on", "in", "to", "and", "or", "my", "i", "what", "how", "much",
         "can", "do", "does", "under", "with", "me", "tell", "about", "any", "are", "which", "there", "it", "be",
         "much", "this", "year", "per", "rs", "rupees", "lakh"}
_ALIASES = {
    "hra": ["house", "rent", "allowance"], "ppf": ["80c"], "elss": ["80c"], "lic": ["80c"], "nps": ["80ccd"],
    "mediclaim": ["80d", "health"], "deadline": ["due", "date"], "itr": ["return", "filing"],
    "harass": ["recovery", "harassment"], "harassing": ["recovery", "harassment"], "agent": ["recovery"],
    "agents": ["recovery"], "complain": ["complaint", "ombudsman"], "foreclose": ["prepayment", "foreclosure"],
    "fd": ["deposit", "interest"], "section": [], "sec": [], "ltcg": ["long", "term", "gains"],
    "stcg": ["short", "term", "gains"], "freelancer": ["presumptive", "44ada"],
}


@dataclass
class Rule:
    id: str
    title: str
    topics: List[str]
    tax_years: List[str]
    summary: str
    citation: Dict[str, str]
    source_url: str
    verified_on: str
    keywords: str = ""
    figures: Dict[str, Any] = field(default_factory=dict)
    regime: Optional[str] = None
    notes: Optional[str] = None

    def applies_to(self, tax_year: Optional[str]) -> bool:
        return tax_year is None or "all" in self.tax_years or tax_year in self.tax_years

    def citation_for(self, tax_year: Optional[str]) -> str:
        if "all" in self.citation:
            return self.citation["all"]
        if tax_year and tax_year in self.citation:
            return self.citation[tax_year]
        latest = sorted(self.citation)[-1]
        return self.citation[latest]

    def to_result(self, tax_year: Optional[str]) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "figures": self.figures,
            "citation": self.citation_for(tax_year),
            "all_citations": self.citation,
            "regime": self.regime,
            "tax_years": self.tax_years,
            "source": self.source_url,
            "verified_on": self.verified_on,
        }


def _tokens(text: str) -> List[str]:
    out: List[str] = []
    for t in _WORD.findall((text or "").lower()):
        if t in _STOP:
            continue
        out.append(t)
        out.extend(_ALIASES.get(t, []))
    return out


class RulesLibrary:
    def __init__(self, rules: List[Rule], version: str = ""):
        self.rules = rules
        self.version = version
        self._by_id = {r.id: r for r in rules}
        self._docs = {r.id: _tokens(" ".join([r.title, r.summary, r.keywords, " ".join(r.topics),
                                              " ".join(r.citation.values())])) for r in rules}
        n = len(rules)
        df: Dict[str, int] = {}
        for toks in self._docs.values():
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        self._idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
        self._avg = sum(len(v) for v in self._docs.values()) / max(1, n)

    @classmethod
    def load(cls, path: Path = RULES_PATH) -> "RulesLibrary":
        data = json.loads(path.read_text(encoding="utf-8"))
        rules = [Rule(**{k: v for k, v in r.items() if k in Rule.__dataclass_fields__}) for r in data["rules"]]
        return cls(rules, data.get("version", ""))

    def get(self, rule_id: str) -> Optional[Rule]:
        return self._by_id.get(rule_id)

    def search(self, query: str, tax_year: Optional[str] = None, limit: int = 3,
               topic: Optional[str] = None) -> List[Dict[str, Any]]:
        q = _tokens(query)
        if not q:
            return []
        scored = []
        for r in self.rules:
            if not r.applies_to(tax_year) or (topic and topic not in r.topics):
                continue
            doc = self._docs[r.id]
            length = len(doc)
            score = 0.0
            for t in set(q):
                tf = doc.count(t)
                if not tf:
                    continue
                score += self._idf.get(t, 0) * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / self._avg))
            # exact section numbers are strong signals ("80c", "24b", "156")
            for t in set(q):
                if re.fullmatch(r"\d+[a-z]*", t) and any(re.search(rf"\b{re.escape(t)}\b", c.lower().replace("(", "").replace(")", ""))
                                                         for c in r.citation.values()):
                    score += 3.0
            if score > 0:
                scored.append((score, r))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            return []
        top = scored[0][0]
        return [r.to_result(tax_year) for s, r in scored[:limit] if s >= 0.35 * top]

    def check(self, today: Optional[date] = None) -> List[str]:
        """Problems that block a release (structure, staleness, drift from the tax engine)."""
        problems: List[str] = []
        today = today or date.today()
        seen = set()
        for r in self.rules:
            if r.id in seen:
                problems.append(f"{r.id}: duplicate id")
            seen.add(r.id)
            if not r.summary or not r.citation or not r.source_url.startswith("http"):
                problems.append(f"{r.id}: missing summary, citation or source")
            for ty in r.tax_years:
                if ty != "all" and not re.fullmatch(r"\d{4}-\d{2}", ty):
                    problems.append(f"{r.id}: bad tax year {ty}")
            try:
                age = (today - date.fromisoformat(r.verified_on)).days
                if age > STALE_AFTER_DAYS:
                    problems.append(f"{r.id}: last verified {r.verified_on} (over a year ago)")
            except ValueError:
                problems.append(f"{r.id}: bad verified_on date")
        problems.extend(self._engine_drift())
        return problems

    def _engine_drift(self) -> List[str]:
        from ..finance.tax import TAX_RULES

        out = []
        for ty, rules in TAX_RULES.items():
            pairs = [
                ("standard-deduction", "new", rules["new"]["standard_deduction"]),
                ("standard-deduction", "old", rules["old"]["standard_deduction"]),
                ("rebate", "new_limit", rules["new"]["rebate_limit"]),
                ("rebate", "new_max", rules["new"]["rebate_max"]),
                ("rebate", "old_limit", rules["old"]["rebate_limit"]),
                ("rebate", "old_max", rules["old"]["rebate_max"]),
                ("80c", "limit", rules["deduction_caps"]["section_80c"]),
                ("home-loan-interest", "self_occupied_limit", rules["deduction_caps"]["home_loan_interest"]),
                ("ltcg-equity", "exemption", rules["capital_gains"]["ltcg_equity_exemption"]),
                ("ltcg-equity", "rate", rules["capital_gains"]["ltcg_equity"] * 100),
                ("stcg-equity", "rate", rules["capital_gains"]["stcg_equity"] * 100),
                ("surcharge-cess", "cess", rules["cess"] * 100),
            ]
            for rid, key, engine_value in pairs:
                rule = self.get(rid)
                if rule is None or not rule.applies_to(ty):
                    continue
                lib_value = rule.figures.get(key)
                if lib_value is None or abs(float(lib_value) - float(engine_value)) > 1e-6:
                    out.append(f"{rid}.{key} = {lib_value} but the tax engine uses {engine_value} for {ty}")
            slabs = [[None if u == float("inf") else u, round(r * 100, 4)] for u, r in rules["new"]["slabs"]["normal"]]
            lib_slabs = [[u, float(r)] for u, r in (self.get("new-regime-slabs").figures.get("slabs") or [])]
            if slabs != lib_slabs:
                out.append(f"new-regime-slabs differ from the tax engine for {ty}")
        return out


@lru_cache(maxsize=1)
def get_library() -> RulesLibrary:
    return RulesLibrary.load()
