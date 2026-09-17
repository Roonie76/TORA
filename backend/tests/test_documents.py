"""Phase 11: reading Form 16, salary slips, bank statements and AIS."""
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
from backend.documents import DocumentError, detect_type, extract_text, mask_identifiers, parse_document
from backend.tests.test_sessions_api import ScriptedLLM

FIX = Path(__file__).parent / "fixtures" / "documents"


def read(name):
    return (FIX / name).read_bytes()


def facts(doc):
    return {f["name"]: f["value"] for f in doc.proposed_facts}


def test_form16():
    doc = parse_document(read("form16.txt"), "form16.txt")
    s = doc.summary
    assert doc.doc_type == "form16" and doc.confidence == "high"
    assert s["gross_salary"] == 1250000 and s["hra_exempt"] == 120000
    assert s["standard_deduction"] == 50000 and s["professional_tax"] == 2500
    assert s["section_80c"] == 150000 and s["section_80ccd_1b"] == 50000 and s["section_80d"] == 25000
    assert s["taxable_income"] == 852500 and s["tds_deducted"] == 128960
    assert s["assessment_year"] == "2026-27" and s["tax_year"] == "2025-26" and s["regime"] == "old"
    assert facts(doc) == {"income": pytest.approx(104166.67), "section_80c_invested": 150000}


def test_salary_slip():
    doc = parse_document(read("salary_slip.txt"), "slip.txt")
    s = doc.summary
    assert doc.doc_type == "salary_slip" and s["month"] == "August 2026"
    assert (s["basic"], s["hra"], s["gross_earnings"], s["net_pay"]) == (50000, 20000, 100000, 84000)
    assert (s["pf"], s["professional_tax"], s["income_tax"], s["total_deductions"]) == (6000, 200, 9800, 16000)
    assert doc.warnings == []
    assert facts(doc) == {"income": 84000, "epf_monthly": 6000}


def test_salary_slip_inconsistency_is_flagged():
    text = read("salary_slip.txt").decode().replace("Net Pay                          84,000.00",
                                                     "Net Pay                          80,000.00")
    doc = parse_document(text.encode(), "slip.txt")
    assert any("does not equal net pay" in w for w in doc.warnings)


def test_bank_statement_csv():
    doc = parse_document(read("bank_statement.csv"), "statement.csv")
    s = doc.summary
    assert doc.doc_type == "bank_statement"
    assert s["period"] == {"from": "2026-06-01", "to": "2026-08-12", "months": 3}
    assert s["salary_per_month"] == 84000 and s["total_credits"] == 252000
    assert s["recurring_emis"] == [{"lender": "bajaj finance", "monthly": 12000, "months_seen": 3}]
    assert s["spend_by_category"]["rent"] == 66000 and s["bank_charges"] == 590
    assert any("charges" in w for w in doc.warnings)
    assert facts(doc) == {"income": 84000, "bank_emi_total": 12000, "rent": 22000, "savings": 139860}


def test_bank_statement_pdf_style_text_uses_balances_for_signs():
    doc = parse_document(read("bank_statement.txt"), "statement.txt")
    s = doc.summary
    assert s["transactions"] == 6 and s["total_credits"] == 168000 and s["total_debits"] == 26300
    assert s["recurring_emis"][0]["monthly"] == 9500
    assert s["closing_balance"] == 151700


def test_real_pdf_and_password(tmp_path):
    reportlab = pytest.importorskip("reportlab")
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from pypdf import PdfReader, PdfWriter

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for line in read("salary_slip.txt").decode().splitlines():
        c.drawString(40, y, line)
        y -= 16
    c.save()
    plain = buf.getvalue()
    assert parse_document(plain, "slip.pdf").summary["net_pay"] == 84000

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(plain)))
    writer.encrypt("ravi1990")
    locked = io.BytesIO()
    writer.write(locked)
    with pytest.raises(DocumentError, match="password-protected"):
        parse_document(locked.getvalue(), "slip.pdf")
    with pytest.raises(DocumentError, match="incorrect"):
        parse_document(locked.getvalue(), "slip.pdf", password="wrong")
    assert parse_document(locked.getvalue(), "slip.pdf", password="ravi1990").summary["net_pay"] == 84000


@pytest.mark.parametrize("data, name, message", [
    (b"", "a.pdf", "empty"),
    (b"x" * (5 * 1024 * 1024 + 1), "a.csv", "larger than 5 MB"),
    (b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 10, "photo.png", "Unsupported"),
    (b"%PDF-1.4 not really a pdf", "a.pdf", "could not be read"),
    (b"hello there, nothing financial here", "note.txt", "couldn't tell"),
])
def test_bad_files(data, name, message):
    with pytest.raises(DocumentError, match=message):
        parse_document(data, name)


def test_masking_and_detection():
    masked = mask_identifiers("PAN ABCPK1234Q a/c 50100123456789 mail r@x.in +91 9876543210")
    assert "ABCPK1234Q" not in masked and "XXXXX1234X" in masked
    assert "50100123456789" not in masked and "6789" in masked
    assert "r@x.in" not in masked and "9876543210" not in masked
    assert detect_type("Annual Information Statement (AIS)") == "ais"
    assert detect_type("random text") == "unknown"


def test_ais_totals():
    text = ("Annual Information Statement (AIS)\nInterest from savings bank 8,450.00\n"
            "Interest from deposit 42,300.00\nDividend 3,100.00\n")
    doc = parse_document(text.encode(), "ais.txt")
    assert doc.doc_type == "ais" and doc.summary["total_interest"] == 50750
    assert doc.proposed_facts == [] and doc.warnings


# ── API ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    fake = ScriptedLLM()
    monkeypatch.setattr(main_module.agent, "llm_provider", fake)
    monkeypatch.setattr(main_module.planner, "llm_provider", fake)
    return TestClient(main_module.app), fake


def upload(c, name, conversation_id=None, **extra):
    data = {"conversation_id": conversation_id} if conversation_id else {}
    data.update(extra)
    return c.post("/api/documents", files={"file": (name, read(name))}, data=data)


def test_upload_confirm_and_use_in_chat(client):
    c, fake = client
    r = upload(c, "bank_statement.csv")
    assert r.status_code == 200
    body = r.json()
    cid, doc = body["conversation_id"], body["document"]
    assert doc["doc_type"] == "bank_statement" and doc["status"] == "pending"
    conv = c.get(f"/api/conversations/{cid}").json()
    assert "84,000" not in conv["memory_summary"]          # nothing saved before confirmation

    r = c.post(f"/api/documents/{doc['id']}/confirm", json={"conversation_id": cid, "facts": ["income", "rent"]})
    assert r.status_code == 200 and sorted(r.json()["saved"]) == ["income", "rent"]
    mem = c.get(f"/api/conversations/{cid}").json()["memory_summary"]
    assert "84,000" in mem and "22,000" in mem and "1,39,860" not in mem

    c.post("/api/chat", json={"conversation_id": cid, "message": "What did my bank statement show?"})
    prompt = " ".join(m["content"] for m in fake.answer_calls[-1])
    assert "bajaj finance" in prompt and "Documents the user uploaded" in prompt
    system = fake.answer_calls[-1][0]["content"]
    assert "bajaj finance" not in system   # document text stays in the untrusted block


def test_upload_errors_and_dismiss(client):
    c, _ = client
    bad = c.post("/api/documents", files={"file": ("x.png", b"\x89PNG" + bytes(range(256)) * 10)})
    assert bad.status_code == 422
    r = upload(c, "salary_slip.txt").json()
    cid, doc_id = r["conversation_id"], r["document"]["id"]
    assert upload(c, "form16.txt", conversation_id=cid).json()["conversation_id"] == cid
    assert c.post("/api/documents/nope/confirm", json={"conversation_id": cid}).status_code == 404
    assert c.post(f"/api/documents/{doc_id}/dismiss", json={"conversation_id": cid}).json() == {"dismissed": True}
    state = c.get(f"/api/conversations/{cid}").json()["state"]
    assert [d["doc_type"] for d in state["documents"]] == ["form16"]
    assert upload(c, "form16.txt", conversation_id="x" * 32).status_code == 404
    assert c.post("/api/documents", files={"file": ("a.txt", b"hi")}, data={"doc_type": "passport"}).status_code == 422


def test_owned_documents_are_private(client, monkeypatch):
    from backend.auth import StaticAuthVerifier
    c, _ = client
    monkeypatch.setenv("TORA_AUTH_MODE", "optional")
    monkeypatch.setattr(main_module, "auth_verifier", StaticAuthVerifier({"ta": "alice", "tb": "bob"}))
    r = c.post("/api/documents", files={"file": ("slip.txt", read("salary_slip.txt"))},
               headers={"Authorization": "Bearer ta"}).json()
    cid, doc_id = r["conversation_id"], r["document"]["id"]
    assert c.post(f"/api/documents/{doc_id}/confirm", json={"conversation_id": cid},
                  headers={"Authorization": "Bearer tb"}).status_code == 404
    ok = c.post(f"/api/documents/{doc_id}/confirm", json={"conversation_id": cid},
                headers={"Authorization": "Bearer ta"})
    assert ok.status_code == 200
    assert "84,000" in c.get("/api/me/memory", headers={"Authorization": "Bearer ta"}).json()["memory_summary"]
    main_module.session_store.delete_user_data("alice")
