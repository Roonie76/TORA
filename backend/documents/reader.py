"""
Document reading (Phase 11).

Turns an uploaded file into a compact, privacy-masked summary plus *proposed*
facts. Nothing is saved to memory until the user confirms. The raw file and the
full text are never stored — only the summary.

Supported: PDF (incl. password-protected), CSV, TXT. Types: Form 16, salary
slip, bank statement, AIS / Form 26AS (basic totals).
"""

from __future__ import annotations

import csv
import io
import re
import statistics
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

MAX_BYTES = 5 * 1024 * 1024
MAX_PAGES = 60
MAX_TEXT = 400_000
_AMOUNT = r"(?:₹|rs\.?|inr)?\s*(?<![\d.,])(-?\d[\d,]*(?:\.\d{1,2})?)(?![\d])"
_PAN = re.compile(r"\b([A-Z]{5})(\d{4})([A-Z])\b")
_LONG_NUMBER = re.compile(r"\b(\d{4,})(\d{4})\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
_PHONE = re.compile(r"(?<!\d)(?:\+91[\s-]?)?[6-9]\d{9}(?!\d)")


class DocumentError(Exception):
    """The file cannot be read (type, size, password, empty)."""


@dataclass
class ParsedDocument:
    doc_type: str
    filename: str
    summary: Dict[str, Any]
    proposed_facts: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    confidence: str = "medium"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    uploaded_at: str = field(default_factory=lambda: datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    status: str = "pending"  # pending | confirmed | dismissed

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "doc_type": self.doc_type, "filename": self.filename, "summary": self.summary,
                "proposed_facts": self.proposed_facts, "warnings": self.warnings, "confidence": self.confidence,
                "uploaded_at": self.uploaded_at, "status": self.status}


# ───────────────────────────────────────────────────────────── text extraction

def mask_identifiers(text: str) -> str:
    text = _PAN.sub(lambda m: "XXXXX" + m.group(2) + "X", text)
    text = _LONG_NUMBER.sub(lambda m: "X" * len(m.group(1)) + m.group(2), text)
    text = _EMAIL.sub("[email]", text)
    return _PHONE.sub("[phone]", text)


def extract_text(data: bytes, filename: str, password: Optional[str] = None) -> str:
    if not data:
        raise DocumentError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise DocumentError("The file is larger than 5 MB.")
    name = (filename or "").lower()
    if data[:5] == b"%PDF-" or name.endswith(".pdf"):
        return _pdf_text(data, password)
    if name.endswith((".csv", ".txt")) or _looks_like_text(data):
        for enc in ("utf-8-sig", "utf-16", "latin-1"):
            try:
                return data.decode(enc)[:MAX_TEXT]
            except UnicodeDecodeError:
                continue
    raise DocumentError("Unsupported file type. Upload a PDF, CSV or TXT file.")


def _looks_like_text(data: bytes) -> bool:
    sample = data[:2000]
    return bool(sample) and sum(32 <= b < 127 or b in (9, 10, 13) for b in sample) / len(sample) > 0.9


def _pdf_text(data: bytes, password: Optional[str]) -> str:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:  # pragma: no cover
        raise DocumentError("PDF support is not installed (pip install pypdf).") from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            if not password:
                raise DocumentError("This PDF is password-protected. Enter its password to read it.")
            try:
                ok = reader.decrypt(password)
            except Exception as exc:  # unsupported encryption
                raise DocumentError("This PDF uses encryption that cannot be opened here.") from exc
            if not ok:
                raise DocumentError("The PDF password is incorrect.")
        if len(reader.pages) > MAX_PAGES:
            raise DocumentError(f"The PDF has more than {MAX_PAGES} pages.")
        parts = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
            if sum(len(p) for p in parts) > MAX_TEXT:
                break
    except DocumentError:
        raise
    except (PdfReadError, ValueError, KeyError, OSError) as exc:
        raise DocumentError("The PDF could not be read.") from exc
    text = "\n".join(parts)
    if not text.strip():
        raise DocumentError("No text found — this looks like a scanned image. Upload the original PDF or a CSV.")
    return text[:MAX_TEXT]


# ───────────────────────────────────────────────────────────── helpers

def _num(raw: str) -> Optional[float]:
    try:
        return float(raw.replace(",", "").replace("₹", "").strip())
    except (ValueError, AttributeError):
        return None


def _amount_for(text: str, *labels: str, last: bool = True) -> Optional[float]:
    """Amount on the same line as the label (the last amount on that line by default)."""
    for label in labels:
        for line in text.splitlines():
            if re.search(label, line, re.IGNORECASE):
                tail = re.split(label, line, maxsplit=1, flags=re.IGNORECASE)[-1]
                nums = [_num(m) for m in re.findall(_AMOUNT, tail)]
                nums = [n for n in nums if n is not None and not _looks_like_year_or_section(n, tail)]
                if nums:
                    return nums[-1] if last else nums[0]
    return None


def _looks_like_year_or_section(n: float, tail: str) -> bool:
    return (n.is_integer() and 1990 <= n <= 2100 and not re.search(r"\d,\d", tail)) or n in (10, 16, 17, 80, 115)


def detect_type(text: str) -> str:
    t = text.lower()
    if "form no. 16" in t or "form 16" in t or ("part b" in t and "17(1)" in t):
        return "form16"
    if "annual information statement" in t or "form 26as" in t or "annual tax statement" in t:
        return "ais"
    if re.search(r"\b(pay\s*slip|salary\s*slip|payslip|earnings)\b", t) and re.search(r"\b(net\s*pay|net\s*salary|take\s*home)\b", t):
        return "salary_slip"
    if re.search(r"\b(statement of account|account statement|opening balance|closing balance|narration|withdrawal|deposit)\b", t):
        return "bank_statement"
    return "unknown"


# ───────────────────────────────────────────────────────────── Form 16

def parse_form16(text: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[str]]:
    ay = re.search(r"assessment\s+year\s*[:\-]?\s*(20\d{2})\s*[-–]\s*(\d{2,4})", text, re.IGNORECASE)
    fy = re.search(r"(?:financial|tax)\s+year\s*[:\-]?\s*(20\d{2})\s*[-–]\s*(\d{2,4})", text, re.IGNORECASE)
    regime_old = re.search(r"opt(?:ing|ed)?\s+out\s+of\s+taxation\s+under\s+section\s*115BAC[^\n]*?\b(yes|no)\b",
                           text, re.IGNORECASE)
    s = {
        "assessment_year": f"{ay.group(1)}-{ay.group(2)[-2:]}" if ay else None,
        "tax_year": f"{fy.group(1)}-{fy.group(2)[-2:]}" if fy else (
            f"{int(ay.group(1)) - 1}-{ay.group(1)[-2:]}" if ay else None),
        "gross_salary": _amount_for(text, r"salary as per provisions contained in section\s*17\s*\(1\)",
                                    r"gross salary\b[^\n]*total", r"^\s*\(d\)\s*total"),
        "hra_exempt": _amount_for(text, r"house rent allowance[^\n]*10\s*\(13A\)"),
        "total_exempt_allowances": _amount_for(text, r"total amount of exemption claimed under section 10"),
        "standard_deduction": _amount_for(text, r"standard deduction[^\n]*16\s*\(ia\)", r"standard deduction"),
        "professional_tax": _amount_for(text, r"tax on employment[^\n]*16\s*\(iii\)", r"professional tax"),
        "section_80c": _amount_for(text, r"(?:life insurance premia|provident fund)[^\n]*80C\b", r"section 80C\b"),
        "section_80ccd_1b": _amount_for(text, r"80CCD\s*\(1B\)"),
        "section_80d": _amount_for(text, r"health insurance premia[^\n]*80D", r"section 80D\b"),
        "home_loan_interest": _amount_for(text, r"interest on housing loan", r"income \(or admissible loss\) from house property"),
        "taxable_income": _amount_for(text, r"total taxable income"),
        "tax_on_income": _amount_for(text, r"tax on total income"),
        "tds_deducted": _amount_for(text, r"tax deducted at source[^\n]*total", r"total\s*\(rs\.?\)", r"net tax payable"),
        "regime": ("old" if regime_old.group(1).lower() == "yes" else "new") if regime_old else None,
    }
    warnings = []
    if s["gross_salary"] is None:
        warnings.append("Gross salary was not found — please check the figures.")
    facts = []
    if s["gross_salary"]:
        facts.append({"name": "income", "value": round(s["gross_salary"] / 12, 2), "category": "income",
                      "period": "monthly", "label": "Monthly gross salary (from Form 16)",
                      "notes": f"Form 16 gross salary {s['gross_salary']:,.0f} a year"})
    if s["section_80c"]:
        facts.append({"name": "section_80c_invested", "value": s["section_80c"], "category": "investment",
                      "period": "annual", "label": "80C investments claimed (Form 16)"})
    return s, facts, warnings


# ───────────────────────────────────────────────────────────── salary slip

def parse_salary_slip(text: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[str]]:
    month = re.search(r"(?:pay\s*slip|salary\s*slip|payslip)[^\n]*?\b([A-Za-z]{3,9})[\s,'-]*(20\d{2})", text, re.IGNORECASE) \
        or re.search(r"\bfor\s+(?:the\s+month\s+of\s+)?([A-Za-z]{3,9})[\s,'-]*(20\d{2})", text, re.IGNORECASE)
    s = {
        "month": f"{month.group(1).title()} {month.group(2)}" if month else None,
        "basic": _amount_for(text, r"\bbasic(?:\s+salary|\s+pay)?\b", last=False),
        "hra": _amount_for(text, r"\bhouse rent allowance\b|\bhra\b", last=False),
        "gross_earnings": _amount_for(text, r"gross\s+(?:earnings|salary|pay)|total\s+earnings"),
        "pf": _amount_for(text, r"provident fund|\bepf\b|\bpf\b", last=False),
        "professional_tax": _amount_for(text, r"professional tax|\bp\.?\s?tax\b", last=False),
        "income_tax": _amount_for(text, r"income tax|\btds\b", last=False),
        "total_deductions": _amount_for(text, r"total\s+deductions"),
        "net_pay": _amount_for(text, r"net\s+(?:pay|salary)|take\s*home"),
    }
    warnings = []
    if s["net_pay"] is None:
        warnings.append("Net pay was not found — please check the figures.")
    if s["gross_earnings"] and s["total_deductions"] and s["net_pay"]:
        if abs(s["gross_earnings"] - s["total_deductions"] - s["net_pay"]) > 2:
            warnings.append("Gross minus deductions does not equal net pay on this slip — some lines may be missed.")
    facts = []
    if s["net_pay"]:
        facts.append({"name": "income", "value": s["net_pay"], "category": "income", "period": "monthly",
                      "label": "Monthly take-home pay (salary slip)",
                      "notes": f"Salary slip {s['month'] or ''}: gross {s['gross_earnings']}, net {s['net_pay']}".strip()})
    if s["pf"]:
        facts.append({"name": "epf_monthly", "value": s["pf"], "category": "investment", "period": "monthly",
                      "label": "Employee PF contribution per month"})
    return s, facts, warnings


# ───────────────────────────────────────────────────────────── bank statement

_DATE_RE = re.compile(r"^\s*(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{1,2}[\s\-][A-Za-z]{3}[\s\-]\d{2,4}|\d{4}-\d{2}-\d{2})\b")
_CATEGORY_WORDS = [
    ("salary", r"\bsal(?:ary)?\b|payroll"),
    ("charges", r"charges?\b|penalty|bounce|return(?:ed)?\s*chq|insufficient|\breturn\b"),
    ("emi_loan", r"\bemi\b|nach|ecs|loan|\bach\b|bajaj\s*fin|home\s*credit"),
    ("credit_card", r"credit\s*card|\bcc\b|card\s*payment|cred\b"),
    ("rent", r"\brent\b"),
    ("food", r"swiggy|zomato|restaurant|food|blinkit|zepto|bigbasket|grocer"),
    ("shopping", r"amazon|flipkart|myntra|ajio|meesho"),
    ("travel", r"uber|ola\b|irctc|makemytrip|fuel|petrol|hpcl|iocl|bpcl"),
    ("bills", r"electric|bescom|mseb|broadband|airtel|jio|vodafone|\bvi\b|recharge|dth|gas"),
    ("insurance", r"insurance|\blic\b|premium"),
    ("investment", r"\bsip\b|mutual|zerodha|groww|upstox|nps|ppf"),
    ("cash", r"\batm\b|cash\s*wdl|cash\s*withdrawal"),
    ("interest", r"\bint(?:erest)?\b.*(?:cr|credit|paid)|int\.pd|interest"),
]


def _parse_date(raw: str) -> Optional[date]:
    raw = raw.strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y", "%d.%m.%Y", "%d.%m.%y", "%d %b %Y", "%d-%b-%Y",
                "%d %b %y", "%d-%b-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _category(desc: str) -> str:
    d = desc.lower()
    for name, pattern in _CATEGORY_WORDS:
        if re.search(pattern, d):
            return name
    return "other"


def _counterparty(desc: str) -> str:
    d = re.sub(r"[^a-z ]", " ", desc.lower())
    d = re.sub(r"\b(upi|neft|imps|rtgs|nach|ach|ecs|dr|cr|to|from|by|transfer|payment|ref|txn|inb|mb|pos)\b", " ", d)
    words = [w for w in d.split() if len(w) > 2]
    return " ".join(words[:2]) or "unknown"


def _rows_from_csv(text: str) -> List[Dict[str, Any]]:
    sample = text[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    lines = text.splitlines()
    # find the header row
    start = 0
    for i, line in enumerate(lines[:40]):
        low = line.lower()
        if "date" in low and any(k in low for k in ("debit", "withdrawal", "credit", "deposit", "amount")):
            start = i
            break
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])), dialect=dialect)
    rows = []
    for raw in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        dkey = next((k for k in row if "date" in k and "value" not in k), None) or next((k for k in row if "date" in k), None)
        desc_key = next((k for k in row if any(w in k for w in ("narration", "description", "particular", "remark", "details"))), None)
        debit_key = next((k for k in row if any(w in k for w in ("debit", "withdrawal", "dr"))), None)
        credit_key = next((k for k in row if any(w in k for w in ("credit", "deposit", "cr"))), None)
        amt_key = next((k for k in row if k in ("amount", "amount (inr)", "transaction amount")), None)
        bal_key = next((k for k in row if "balance" in k), None)
        when = _parse_date(row.get(dkey, "")) if dkey else None
        if when is None:
            continue
        debit = _num(row.get(debit_key, "")) if debit_key else None
        credit = _num(row.get(credit_key, "")) if credit_key else None
        if amt_key and debit is None and credit is None:
            amt = _num(row[amt_key])
            if amt is not None:
                typ = " ".join(row.values()).lower()
                if amt < 0 or re.search(r"\b(dr|debit)\b", typ):
                    debit = abs(amt)
                else:
                    credit = amt
        amount = (credit or 0.0) - (debit or 0.0)
        if amount == 0:
            continue
        rows.append({"date": when, "description": row.get(desc_key, "") if desc_key else "", "amount": amount,
                     "balance": _num(row.get(bal_key, "")) if bal_key else None})
    return rows


def _rows_from_text(text: str) -> List[Dict[str, Any]]:
    rows = []
    prev_balance: Optional[float] = None
    opening = _amount_for(text, r"opening balance")
    if opening is not None:
        prev_balance = opening
    for line in text.splitlines():
        m = _DATE_RE.match(line)
        if not m:
            continue
        when = _parse_date(m.group(1))
        if when is None:
            continue
        rest = line[m.end():]
        rest = re.sub(r"^\s*(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4})", "", rest)  # value date column
        amounts = list(re.finditer(r"(?<![\w/])(\d{1,3}(?:,\d{2,3})+(?:\.\d{2})|\d+\.\d{2})(?:\s*(Cr|Dr))?(?![\w/])", rest))
        if not amounts:
            continue
        desc = rest[:amounts[0].start()].strip()
        vals = [(_num(a.group(1)), (a.group(2) or "").lower()) for a in amounts]
        balance = vals[-1][0] if len(vals) >= 2 else None
        if len(vals) >= 3:
            debit, credit = vals[-3][0], vals[-2][0]
            amount = (credit or 0) - (debit or 0) if debit and credit else None
            if amount is None:
                amt = debit or credit
                amount = _signed_by_balance(amt, balance, prev_balance, desc)
        else:
            amt, marker = vals[0]
            if marker in ("cr", "dr"):
                amount = amt if marker == "cr" else -amt
            else:
                amount = _signed_by_balance(amt, balance, prev_balance, desc)
        if amount:
            rows.append({"date": when, "description": desc, "amount": amount, "balance": balance})
        if balance is not None:
            prev_balance = balance
    return rows


def _signed_by_balance(amt: float, balance: Optional[float], prev: Optional[float], desc: str) -> float:
    if balance is not None and prev is not None:
        if abs(prev + amt - balance) < 0.02:
            return amt
        if abs(prev - amt - balance) < 0.02:
            return -amt
    return amt if re.search(r"\b(cr|credit|salary|refund|interest|received|deposit)\b", desc.lower()) else -amt


def parse_bank_statement(text: str, is_csv: bool) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[str]]:
    rows = _rows_from_csv(text) if is_csv else _rows_from_text(text)
    warnings = []
    if not rows:
        raise DocumentError("No transactions were found in this statement.")
    rows.sort(key=lambda r: r["date"])
    months = sorted({r["date"].strftime("%Y-%m") for r in rows})
    per_month = defaultdict(lambda: {"credits": 0.0, "debits": 0.0})
    cats = defaultdict(float)
    salary_by_month = defaultdict(float)
    emi_parties: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    charges = 0.0
    for r in rows:
        m = r["date"].strftime("%Y-%m")
        cat = _category(r["description"])
        if r["amount"] > 0:
            per_month[m]["credits"] += r["amount"]
            if cat == "salary":
                salary_by_month[m] += r["amount"]
        else:
            per_month[m]["debits"] += -r["amount"]
            cats[cat] += -r["amount"]
            if cat == "emi_loan":
                emi_parties[_counterparty(r["description"])].append((m, -r["amount"]))
            if cat == "charges":
                charges += -r["amount"]
    # recurring EMIs: same counterparty in at least 2 months with similar amounts
    emis = []
    for party, items in emi_parties.items():
        by_month = defaultdict(float)
        for mm, amt in items:
            by_month[mm] += amt
        if len(by_month) >= 2 or len(months) == 1:
            typical = statistics.median(by_month.values())
            emis.append({"lender": party, "monthly": round(typical, 2), "months_seen": len(by_month)})
    full_months = [m for m in months if m not in (months[0], months[-1])] or months
    avg_debits = statistics.mean(per_month[m]["debits"] for m in full_months)
    salary_months = [salary_by_month[m] for m in months if salary_by_month[m] > 0]
    balances = [r["balance"] for r in rows if r["balance"] is not None]
    summary = {
        "period": {"from": rows[0]["date"].isoformat(), "to": rows[-1]["date"].isoformat(), "months": len(months)},
        "transactions": len(rows),
        "total_credits": round(sum(p["credits"] for p in per_month.values()), 2),
        "total_debits": round(sum(p["debits"] for p in per_month.values()), 2),
        "by_month": [{"month": m, "credits": round(per_month[m]["credits"], 2),
                      "debits": round(per_month[m]["debits"], 2)} for m in months],
        "average_monthly_spend": round(avg_debits, 2),
        "salary_per_month": round(statistics.median(salary_months), 2) if salary_months else None,
        "recurring_emis": sorted(emis, key=lambda e: -e["monthly"]),
        "spend_by_category": {k: round(v, 2) for k, v in sorted(cats.items(), key=lambda kv: -kv[1])},
        "bank_charges": round(charges, 2),
        "lowest_balance": min(balances) if balances else None,
        "closing_balance": balances[-1] if balances else None,
    }
    if charges:
        warnings.append(f"Bank charges or penalties of {charges:,.0f} were found — check for bounced EMIs or low-balance fees.")
    if summary["lowest_balance"] is not None and summary["lowest_balance"] < 0:
        warnings.append("The balance went negative (overdraft).")
    facts = []
    if summary["salary_per_month"]:
        facts.append({"name": "income", "value": summary["salary_per_month"], "category": "income",
                      "period": "monthly", "label": "Monthly salary credited (bank statement)"})
    total_emi = round(sum(e["monthly"] for e in summary["recurring_emis"]), 2)
    if total_emi:
        facts.append({"name": "bank_emi_total", "value": total_emi, "category": "loan", "period": "monthly",
                      "label": "Total EMIs seen in the bank statement"})
    if summary["spend_by_category"].get("rent"):
        facts.append({"name": "rent", "value": round(summary["spend_by_category"]["rent"] / len(months), 2),
                      "category": "rent", "period": "monthly", "label": "Average monthly rent paid"})
    if summary["closing_balance"] is not None and summary["closing_balance"] > 0:
        facts.append({"name": "savings", "value": summary["closing_balance"], "category": "savings",
                      "period": "lump_sum", "label": "Closing balance in this account"})
    return summary, facts, warnings


# ───────────────────────────────────────────────────────────── AIS / 26AS

def parse_ais(text: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[str]]:
    s = {
        "salary": _amount_for(text, r"salary\b[^\n]*(?:192|393|392)?"),
        "savings_interest": _amount_for(text, r"interest from savings"),
        "deposit_interest": _amount_for(text, r"interest from deposit"),
        "dividend": _amount_for(text, r"\bdividend\b"),
        "securities_sold": _amount_for(text, r"sale of securities|sale of listed"),
        "tds_total": _amount_for(text, r"total tax deducted|tds\s*/\s*tcs total|total tds"),
    }
    warnings = ["AIS/26AS layouts vary — treat these totals as a starting point and compare with the portal."]
    s["total_interest"] = round((s["savings_interest"] or 0) + (s["deposit_interest"] or 0), 2) or None
    return s, [], warnings


# ───────────────────────────────────────────────────────────── entry point

def parse_document(data: bytes, filename: str, password: Optional[str] = None,
                   doc_type: Optional[str] = None) -> ParsedDocument:
    text = extract_text(data, filename, password)
    is_csv = (filename or "").lower().endswith(".csv") or (text.count(",") > 20 and "\n" in text and not data.startswith(b"%PDF"))
    kind = doc_type or detect_type(text)
    if kind == "unknown" and is_csv:
        kind = "bank_statement"
    if kind == "form16":
        summary, facts, warnings = parse_form16(text)
    elif kind == "salary_slip":
        summary, facts, warnings = parse_salary_slip(text)
    elif kind == "bank_statement":
        summary, facts, warnings = parse_bank_statement(text, is_csv)
    elif kind == "ais":
        summary, facts, warnings = parse_ais(text)
    else:
        raise DocumentError("I couldn't tell what this document is. Supported: Form 16, salary slip, bank statement, AIS.")
    found = sum(1 for v in summary.values() if v not in (None, [], {}))
    confidence = "high" if found >= 6 and not warnings else ("medium" if found >= 3 else "low")
    for f in facts:
        f["source"] = "document"
    return ParsedDocument(doc_type=kind, filename=mask_identifiers(filename or "document")[:80],
                          summary=summary, proposed_facts=facts, warnings=warnings, confidence=confidence)
