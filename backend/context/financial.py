from enum import Enum
from typing import Optional, List, Dict, Any, Union
from dataclasses import dataclass, field
from datetime import datetime, timezone


def format_inr_large(amount: float) -> str:
    """Format amounts of ₹1 lakh and above in Indian units (Lakh / Crore)."""
    unit, divisor = ("Crore", 10000000) if amount >= 10000000 else ("Lakh", 100000)
    scaled = amount / divisor
    if scaled == int(scaled):
        return f"₹{int(scaled)} {unit}"
    formatted = f"{scaled:.2f}".rstrip("0").rstrip(".")
    return f"₹{formatted} {unit}"


class FactStatus(str, Enum):
    CURRENT = "current"
    HISTORICAL = "historical"
    HYPOTHETICAL = "hypothetical"
    CONDITIONAL = "conditional"
    ESTIMATE = "estimate"
    UNKNOWN = "unknown"
    AMBIGUOUS = "ambiguous"
    # Memory 2.0: a value the user said was wrong (never used as previous/original)
    RETRACTED = "retracted"


@dataclass
class FactRevision:
    """Represents a historical revision point in the lifecycle of a financial fact."""
    value: Any
    status: str = FactStatus.HISTORICAL.value
    source: str = "user"
    notes: Optional[str] = None
    timestamp: Optional[str] = None
    turn: Optional[int] = None

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "value": self.value,
            "status": self.status,
            "source": self.source,
            "notes": self.notes,
            "timestamp": self.timestamp,
            "turn": self.turn,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FactRevision":
        return cls(
            value=data.get("value"),
            status=data.get("status", FactStatus.HISTORICAL.value),
            source=data.get("source", "user"),
            notes=data.get("notes"),
            timestamp=data.get("timestamp"),
            turn=data.get("turn"),
        )

    def format_value(self, period: Optional[str] = None) -> str:
        """Format the revision value with proper currency / unit notation."""
        if isinstance(self.value, (int, float)):
            if period == "interest_rate":
                return f"{self.value}%"
            if self.value >= 100000:
                return format_inr_large(self.value)
            return f"₹{self.value:,.0f}"
        return str(self.value)


@dataclass
class FinancialFact:
    """
    Strongly typed representation of an individual financial fact.
    Tracks full multi-hop provenance, semantic status, and historical revision chains.
    """
    name: str
    value: Any
    currency: str = "INR"
    period: Optional[str] = None  # "monthly", "annual", "lump_sum", "interest_rate", etc.
    status: str = FactStatus.CURRENT.value
    revisions: List[FactRevision] = field(default_factory=list)
    source: str = "user"
    notes: Optional[str] = None
    updated_at: Optional[str] = None
    source_turn: Optional[int] = None

    def __post_init__(self):
        if not self.updated_at:
            self.updated_at = datetime.now(timezone.utc).isoformat()

    def _valid_revisions(self) -> List[FactRevision]:
        """Revisions that were true at some point (excludes values the user retracted)."""
        return [r for r in self.revisions if r.status != FactStatus.RETRACTED.value]

    def get_retracted_values(self) -> List[Any]:
        """Values the user explicitly said were wrong."""
        return [r.value for r in self.revisions if r.status == FactStatus.RETRACTED.value]

    @property
    def previous_value(self) -> Optional[Any]:
        """Backwards-compatible accessor for immediate previous value."""
        return self.get_previous_value()

    @previous_value.setter
    def previous_value(self, val: Any):
        if val is not None:
            if not self.revisions or self.revisions[-1].value != val:
                self.revisions.append(FactRevision(value=val, status=FactStatus.HISTORICAL.value, source=self.source))

    def get_current_value(self) -> Any:
        """Return the current active value of the fact."""
        return self.value

    def get_previous_value(self) -> Optional[Any]:
        """Return the immediate previous value prior to the current value, if any."""
        valid = self._valid_revisions()
        if valid:
            return valid[-1].value
        return None

    def get_original_value(self) -> Any:
        """Return the earliest recorded (non-retracted) value in the revision chain."""
        valid = self._valid_revisions()
        if valid:
            return valid[0].value
        return self.value

    def get_provenance_chain(self) -> List[Any]:
        """Return full chronological chain of true values from original to current."""
        chain = [rev.value for rev in self._valid_revisions()]
        if not chain or chain[-1] != self.value:
            chain.append(self.value)
        return chain

    def add_revision(
        self,
        old_val: Any,
        status: str = FactStatus.HISTORICAL.value,
        notes: Optional[str] = None,
        turn: Optional[int] = None,
    ):
        """Append an earlier revision to this fact's history."""
        if self.revisions and self.revisions[-1].value == old_val and self.revisions[-1].status == status:
            return
        self.revisions.append(FactRevision(
            value=old_val,
            status=status,
            source=self.source,
            notes=notes,
            turn=turn,
        ))

    def apply_update(
        self,
        value: Any,
        period: Optional[str],
        notes: Optional[str],
        default_period: Optional[str] = None,
        is_correction: bool = False,
        turn: Optional[int] = None,
    ) -> None:
        """
        Move the current value into history and set a new current value.
        A correction marks the old value as RETRACTED (it was never true), so it is
        excluded from previous/original values and the provenance chain.
        """
        if self.value != value:
            old_status = FactStatus.RETRACTED.value if is_correction else FactStatus.HISTORICAL.value
            self.add_revision(self.value, status=old_status, turn=self.source_turn)
        self.value = value
        self.period = period or self.period or default_period
        self.status = FactStatus.CURRENT.value
        self.notes = notes
        self.source_turn = turn
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "currency": self.currency,
            "period": self.period,
            "status": self.status,
            "previous_value": self.get_previous_value(),
            "original_value": self.get_original_value(),
            "revisions": [r.to_dict() for r in self.revisions],
            "source": self.source,
            "notes": self.notes,
            "updated_at": self.updated_at,
            "source_turn": self.source_turn,
            "retracted_values": self.get_retracted_values(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FinancialFact":
        return cls(
            name=data["name"],
            value=data.get("value"),
            currency=data.get("currency", "INR"),
            period=data.get("period"),
            status=data.get("status", FactStatus.CURRENT.value),
            revisions=[FactRevision.from_dict(r) for r in data.get("revisions", [])],
            source=data.get("source", "user"),
            notes=data.get("notes"),
            updated_at=data.get("updated_at"),
            source_turn=data.get("source_turn"),
        )

    def format_value(self, val: Optional[Any] = None) -> str:
        """Format value with currency and unit notation cleanly."""
        target_val = self.value if val is None else val
        if isinstance(target_val, (int, float)):
            if self.period == "interest_rate":
                return f"{target_val}%"
            if target_val >= 100000:
                return format_inr_large(target_val)
            return f"₹{target_val:,.0f}"
        return str(target_val)


@dataclass
class FinancialProfile:
    """
    Structured container for verified user financial profile.
    Maintains active facts, historical changes, debts, assets, goals, and hypothetical scenarios.
    """
    income: Optional[FinancialFact] = None
    rent: Optional[FinancialFact] = None
    expenses: Dict[str, FinancialFact] = field(default_factory=dict)
    loans: Dict[str, FinancialFact] = field(default_factory=dict)
    debts: Dict[str, FinancialFact] = field(default_factory=dict)
    savings: Optional[FinancialFact] = None
    investments: Dict[str, FinancialFact] = field(default_factory=dict)
    goals: Dict[str, FinancialFact] = field(default_factory=dict)
    scenarios: List[FinancialFact] = field(default_factory=list)
    assumptions: List[FinancialFact] = field(default_factory=list)
    history: List[Dict[str, Any]] = field(default_factory=list)

    # Backwards compatibility attributes for Phase 1 FinancialContext tests
    @property
    def monthly_income(self) -> Optional[float]:
        if self.income and self.income.status == FactStatus.CURRENT.value and isinstance(self.income.value, (int, float)):
            return float(self.income.value)
        return None

    @property
    def monthly_budget(self) -> Optional[float]:
        return None

    @property
    def tax_profile(self) -> Optional[Dict[str, Any]]:
        return None

    @property
    def profile_summary(self) -> Optional[Dict[str, Any]]:
        return None

    @property
    def recent_transactions_summary(self) -> Optional[str]:
        return None

    @property
    def account_summaries(self) -> List[Dict[str, Any]]:
        return []

    @property
    def wealth_summaries(self) -> List[Dict[str, Any]]:
        return []

    def is_empty(self) -> bool:
        """Return True if no financial data is recorded."""
        return (
            self.income is None
            and self.rent is None
            and not self.expenses
            and not self.loans
            and not self.debts
            and self.savings is None
            and not self.investments
            and not self.goals
            and not self.scenarios
            and not self.assumptions
        )

    # Canonical fact names for the singleton slots
    _SINGLETON_ALIASES = {
        "income": "income", "salary": "income",
        "rent": "rent",
        "savings": "savings", "emergency_fund": "savings",
    }
    _SINGLETON_DEFAULT_PERIOD = {"income": "monthly", "rent": "monthly", "savings": "lump_sum"}

    def _dict_for(self, clean_name: str, category: str) -> Dict[str, FinancialFact]:
        if category == "expense":
            return self.expenses
        if category == "loan":
            return self.loans
        if category == "debt":
            return self.debts
        if category == "investment":
            return self.investments
        if category == "goal":
            return self.goals
        # Infer category
        if "emi" in clean_name or "loan" in clean_name:
            return self.loans
        if clean_name in ("fixed_deposit", "stocks", "ppf", "epf", "sip_monthly", "mutual_funds", "gold"):
            return self.investments
        if any(w in clean_name for w in ("card", "debt", "apr", "interest", "balance")):
            return self.debts
        if any(w in clean_name for w in ("mutual", "gold", "stock")):
            return self.investments
        if any(w in clean_name for w in ("goal", "car", "house")):
            return self.goals
        return self.expenses

    def set_fact(
        self,
        name: str,
        value: Any,
        category: str = "general",
        period: Optional[str] = None,
        status: str = FactStatus.CURRENT.value,
        source: str = "user",
        notes: Optional[str] = None,
        turn: Optional[int] = None,
        is_correction: bool = False,
    ) -> FinancialFact:
        """
        Record or update a financial fact with multi-hop history tracking.
        Guarantees that HYPOTHETICAL or CONDITIONAL values never overwrite current state.
        `is_correction` marks the replaced value as retracted (the user said it was wrong).
        """
        clean_name = name.strip().lower()
        fact_status = status.lower() if isinstance(status, str) else status

        # 1. Non-current facts (HYPOTHETICAL / CONDITIONAL / ASSUMPTION) are strictly isolated
        if category == "assumption" or fact_status in (FactStatus.HYPOTHETICAL.value, FactStatus.CONDITIONAL.value, FactStatus.ESTIMATE.value):
            target_status = FactStatus.HYPOTHETICAL.value if (category == "assumption" and fact_status == FactStatus.CURRENT.value) else fact_status
            scenario_fact = FinancialFact(
                name=clean_name,
                value=value,
                period=period,
                status=target_status,
                source=source,
                notes=notes,
                source_turn=turn,
            )
            self.scenarios.append(scenario_fact)
            self.assumptions.append(scenario_fact)
            self.history.append({
                "name": clean_name,
                "value": value,
                "category": category,
                "status": target_status,
                "source": source,
                "notes": notes,
                "turn": turn,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            return scenario_fact

        # 2. Historical facts (explicit past statements)
        if fact_status == FactStatus.HISTORICAL.value:
            existing = self.get_fact(clean_name)
            if existing and existing.status == FactStatus.CURRENT.value:
                existing.add_revision(old_val=value, status=FactStatus.HISTORICAL.value, notes=notes, turn=turn)
                return existing
            hist_fact = FinancialFact(
                name=clean_name,
                value=value,
                period=period,
                status=FactStatus.HISTORICAL.value,
                source=source,
                notes=notes,
                source_turn=turn,
            )
            self.assumptions.append(hist_fact)
            return hist_fact

        # 3. Current active facts update
        slot = self._SINGLETON_ALIASES.get(clean_name)
        if slot is None and category in ("income", "rent", "savings"):
            slot = category
        if slot is not None:
            existing = getattr(self, slot)
            if existing and existing.status == FactStatus.CURRENT.value:
                existing.apply_update(
                    value, period, notes,
                    default_period=self._SINGLETON_DEFAULT_PERIOD[slot],
                    is_correction=is_correction, turn=turn,
                )
                fact = existing
            else:
                fact = FinancialFact(
                    name=slot,
                    value=value,
                    period=period or self._SINGLETON_DEFAULT_PERIOD[slot],
                    status=FactStatus.CURRENT.value,
                    source=source,
                    notes=notes,
                    source_turn=turn,
                )
                setattr(self, slot, fact)
        else:
            target_dict = self._dict_for(clean_name, category)
            existing = target_dict.get(clean_name)
            if existing and existing.status == FactStatus.CURRENT.value:
                existing.apply_update(value, period, notes, is_correction=is_correction, turn=turn)
                fact = existing
            else:
                fact = FinancialFact(
                    name=clean_name,
                    value=value,
                    period=period,
                    status=FactStatus.CURRENT.value,
                    source=source,
                    notes=notes,
                    source_turn=turn,
                )
                target_dict[clean_name] = fact

        # Record event in audit history
        self.history.append({
            "name": clean_name,
            "new_value": value,
            "previous_value": fact.get_previous_value(),
            "original_value": fact.get_original_value(),
            "category": category,
            "status": FactStatus.CURRENT.value,
            "correction": is_correction,
            "source": source,
            "turn": turn,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        return fact

    def delete_fact(self, name: str, turn: Optional[int] = None) -> bool:
        """
        Forget a fact (user request). Removes the active value, its revision history
        and any hypothetical scenarios with the same name. Only a value-free tombstone
        is kept in the audit history.
        """
        clean = name.strip().lower()
        removed = False
        slot = self._SINGLETON_ALIASES.get(clean)
        if slot is not None:
            if getattr(self, slot) is not None:
                setattr(self, slot, None)
                removed = True
            clean = slot
        else:
            for d in (self.expenses, self.loans, self.debts, self.investments, self.goals):
                if clean in d:
                    del d[clean]
                    removed = True
        before = len(self.scenarios) + len(self.assumptions)
        self.scenarios = [f for f in self.scenarios if f.name != clean]
        self.assumptions = [f for f in self.assumptions if f.name != clean]
        removed = removed or before != len(self.scenarios) + len(self.assumptions)
        if removed:
            # Scrub values from the audit trail as well
            self.history = [h for h in self.history if h.get("name") != clean and h.get("name") != name.strip().lower()]
            self.history.append({
                "name": clean,
                "status": "deleted",
                "turn": turn,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        return removed

    def clear(self, turn: Optional[int] = None) -> None:
        """Forget everything."""
        self.income = None
        self.rent = None
        self.savings = None
        self.expenses = {}
        self.loans = {}
        self.debts = {}
        self.investments = {}
        self.goals = {}
        self.scenarios = []
        self.assumptions = []
        self.history = [{
            "name": "*",
            "status": "deleted",
            "turn": turn,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]

    def iter_current_facts(self):
        """Yield all active (current) facts."""
        for f in (self.income, self.rent, self.savings):
            if f is not None and f.status == FactStatus.CURRENT.value:
                yield f
        for d in (self.expenses, self.loans, self.debts, self.investments, self.goals):
            for f in d.values():
                if f.status == FactStatus.CURRENT.value:
                    yield f

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the full profile (used for server-side session persistence)."""
        def _opt(f: Optional[FinancialFact]):
            return f.to_dict() if f is not None else None

        return {
            "version": 2,
            "income": _opt(self.income),
            "rent": _opt(self.rent),
            "savings": _opt(self.savings),
            "expenses": {k: v.to_dict() for k, v in self.expenses.items()},
            "loans": {k: v.to_dict() for k, v in self.loans.items()},
            "debts": {k: v.to_dict() for k, v in self.debts.items()},
            "investments": {k: v.to_dict() for k, v in self.investments.items()},
            "goals": {k: v.to_dict() for k, v in self.goals.items()},
            "scenarios": [f.to_dict() for f in self.scenarios],
            "assumptions": [f.to_dict() for f in self.assumptions if f not in self.scenarios],
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "FinancialProfile":
        profile = cls()
        if not data:
            return profile

        def _opt(d):
            return FinancialFact.from_dict(d) if d else None

        profile.income = _opt(data.get("income"))
        profile.rent = _opt(data.get("rent"))
        profile.savings = _opt(data.get("savings"))
        for key in ("expenses", "loans", "debts", "investments", "goals"):
            setattr(profile, key, {k: FinancialFact.from_dict(v) for k, v in (data.get(key) or {}).items()})
        profile.scenarios = [FinancialFact.from_dict(v) for v in data.get("scenarios", [])]
        # Scenarios are mirrored into assumptions (legacy behaviour)
        profile.assumptions = list(profile.scenarios) + [
            FinancialFact.from_dict(v) for v in data.get("assumptions", [])
        ]
        profile.history = list(data.get("history", []))
        return profile

    def get_fact(self, name: str) -> Optional[FinancialFact]:
        """Retrieve a fact by name across all categories."""
        clean = name.strip().lower()
        if clean in ("income", "salary"):
            return self.income
        if clean == "rent":
            return self.rent
        if clean in ("savings", "emergency_fund"):
            return self.savings
        for d in (self.expenses, self.loans, self.debts, self.investments, self.goals):
            if clean in d:
                return d[clean]
        for s in self.scenarios:
            if s.name == clean:
                return s
        for a in self.assumptions:
            if a.name == clean:
                return a
        return None

    def to_context_string(self) -> str:
        """
        Format the structured profile into clean, prioritized markdown
        for injection into LLM system prompt context.
        Renders Active Facts, Multi-Hop Historical Provenance, and Hypothetical Scenarios.
        """
        if self.is_empty():
            return ""

        lines = ["## User Verified Financial Profile"]

        # 1. Active Current Facts
        current_facts = []
        if self.income and self.income.status == FactStatus.CURRENT.value:
            prov = ""
            if self.income._valid_revisions():
                orig = self.income.format_value(self.income.get_original_value())
                prev = self.income.format_value(self.income.get_previous_value())
                prov = f" (Original: {orig}, Previous: {prev})" if orig != prev else f" (Previous: {prev})"
            stated = ""
            if self.income.notes and "per year)" in self.income.notes:
                stated = " (" + self.income.notes.rsplit("(", 1)[-1].rstrip(")") + ")"
            current_facts.append(f"- Monthly Income: {self.income.format_value()}{stated}{prov}")

        if self.rent and self.rent.status == FactStatus.CURRENT.value:
            prov = ""
            if self.rent._valid_revisions():
                orig = self.rent.format_value(self.rent.get_original_value())
                prev = self.rent.format_value(self.rent.get_previous_value())
                prov = f" (Original: {orig}, Previous: {prev})" if orig != prev else f" (Previous: {prev})"
            current_facts.append(f"- Monthly Rent: {self.rent.format_value()}{prov}")

        for k, v in self.expenses.items():
            if v.status == FactStatus.CURRENT.value:
                current_facts.append(f"- Expense ({k.capitalize()}): {v.format_value()}")

        for k, v in self.loans.items():
            if v.status == FactStatus.CURRENT.value:
                current_facts.append(f"- Loan/EMI ({k.capitalize()}): {v.format_value()}")

        for k, v in self.debts.items():
            if v.status == FactStatus.CURRENT.value:
                prov = ""
                if v._valid_revisions():
                    orig = v.format_value(v.get_original_value())
                    prev = v.format_value(v.get_previous_value())
                    prov = f" (Original: {orig}, Previous: {prev})" if orig != prev else f" (Previous: {prev})"
                current_facts.append(f"- Debt/Liability ({k.capitalize()}): {v.format_value()}{prov}")

        if self.savings and self.savings.status == FactStatus.CURRENT.value:
            current_facts.append(f"- Liquid Savings: {self.savings.format_value()}")

        for k, v in self.investments.items():
            if v.status == FactStatus.CURRENT.value:
                current_facts.append(f"- Asset/Investment ({k.capitalize()}): {v.format_value()}")

        for k, v in self.goals.items():
            if v.status == FactStatus.CURRENT.value:
                current_facts.append(f"- Target Goal ({k.capitalize()}): {v.format_value()}")

        if current_facts:
            lines.append("### Active Verified Facts (Current Reality)")
            lines.extend(current_facts)

        # 2. Multi-Hop Historical Provenance & State Change Chains
        history_entries = []
        all_facts_with_history = []
        if self.income and self.income._valid_revisions():
            all_facts_with_history.append(("Monthly Income", self.income))
        if self.rent and self.rent._valid_revisions():
            all_facts_with_history.append(("Monthly Rent", self.rent))
        for k, v in self.debts.items():
            if v._valid_revisions():
                all_facts_with_history.append((f"{k.capitalize()} Debt", v))
        for k, v in self.loans.items():
            if v._valid_revisions():
                all_facts_with_history.append((f"{k.capitalize()} Loan", v))
        for k, v in self.expenses.items():
            if v._valid_revisions():
                all_facts_with_history.append((f"{k.capitalize()} Expense", v))

        for label, fact in all_facts_with_history:
            chain = [fact.format_value(val) for val in fact.get_provenance_chain()]
            chain_str = " -> ".join(chain)
            orig_str = fact.format_value(fact.get_original_value())
            curr_str = fact.format_value(fact.get_current_value())
            prev_str = fact.format_value(fact.get_previous_value())
            history_entries.append(
                f"- {label} Provenance: Original was {orig_str}, Previous was {prev_str}, Current is {curr_str} (Full Revision History: {chain_str})"
            )

        if history_entries:
            lines.append("### Historical Facts & Revision Provenance")
            lines.extend(history_entries)

        # 2b. Values the user corrected as wrong (never use these)
        retracted_entries = []
        for fact in self.iter_current_facts():
            wrong = fact.get_retracted_values()
            if wrong:
                label = fact.name.replace("_", " ").capitalize()
                wrong_str = ", ".join(fact.format_value(v) for v in wrong)
                retracted_entries.append(
                    f"- {label}: {wrong_str} was stated by mistake and corrected to {fact.format_value()} — do not use it."
                )
        if retracted_entries:
            lines.append("### Corrected Mistakes (Invalid Values)")
            lines.extend(retracted_entries)

        # 3. Hypothetical Scenarios & Conditional Assumptions (Isolated)
        hypo_entries = []
        seen_scenarios = set()
        for s in self.scenarios + self.assumptions:
            key = (s.name, s.value, s.status)
            if key in seen_scenarios or s.status == FactStatus.CURRENT.value:
                continue
            seen_scenarios.add(key)
            tag = "[HYPOTHETICAL]" if s.status == FactStatus.HYPOTHETICAL.value else f"[{s.status.upper()}]"
            notes_str = f" ({s.notes})" if s.notes else ""
            hypo_entries.append(f"- {tag} {s.name.capitalize()}: {s.format_value()}{notes_str}")

        if hypo_entries:
            lines.append("### Hypothetical Scenarios & Unconfirmed Assumptions")
            lines.extend(hypo_entries)

        return "\n".join(lines)


# Backwards compatibility adapter for FinancialContext in earlier test suites
FinancialContext = FinancialProfile
