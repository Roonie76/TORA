"""Phase 13: opt-in training log, feedback endpoint and export."""
import json

import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
from backend.observability import training_log
from backend.tests.test_sessions_api import ScriptedLLM


@pytest.fixture
def client(monkeypatch):
    fake = ScriptedLLM()
    monkeypatch.setattr(main_module.agent, "llm_provider", fake)
    monkeypatch.setattr(main_module.planner, "llm_provider", fake)
    return TestClient(main_module.app)


def test_nothing_logged_by_default(client, tmp_path, monkeypatch):
    monkeypatch.delenv("TORA_TRAINING_LOG", raising=False)
    cid = client.post("/api/chat", json={"message": "My PAN is ABCPK1234Q and salary 80k"}).json()["conversation_id"]
    r = client.post("/api/feedback", json={"conversation_id": cid, "turn": 1, "rating": "up"})
    assert r.json() == {"recorded": True, "training_log": False}
    assert list(tmp_path.iterdir()) == []


def test_turns_and_feedback_are_logged_masked_and_exported(client, tmp_path, monkeypatch):
    log = tmp_path / "train.jsonl"
    monkeypatch.setenv("TORA_TRAINING_LOG", str(log))
    monkeypatch.setenv("TORA_FAST_PATH", "on")
    cid = client.post("/api/chat", json={"message": "My PAN is ABCPK1234Q, call me on 9876543210"}).json()["conversation_id"]
    client.post("/api/chat", json={"conversation_id": cid, "message": "EMI on 30 lakh loan at 8.4% for 25 yrs"})
    client.post("/api/chat", json={"conversation_id": cid, "message": "thanks"})
    assert client.post("/api/feedback", json={"conversation_id": cid, "turn": 2, "rating": "up"}).status_code == 200
    client.post("/api/feedback", json={"conversation_id": cid, "turn": 3, "rating": "down",
                                       "better_answer": "You're welcome! Want to plan prepayments next?"})
    assert client.post("/api/feedback", json={"conversation_id": cid, "turn": 9, "rating": "up"}).status_code == 404
    assert client.post("/api/feedback", json={"conversation_id": cid, "turn": 1, "rating": "meh"}).status_code == 422

    lines = [json.loads(l) for l in log.read_text().splitlines()]
    raw = log.read_text()
    assert "ABCPK1234Q" not in raw and "9876543210" not in raw and cid not in raw
    turns = [l for l in lines if l["type"] == "turn"]
    assert len(turns) == 3
    emi = turns[1]
    assert emi["plan_source"] == "fast_path" and emi["plan"][0]["tool"] == "finance_calc"
    assert '"emi": 23954.98' in emi["tools"][0]["output"]
    meta = client.get(f"/api/conversations/{cid}").json()["turns"]
    assert [t["meta"]["feedback"]["rating"] for t in meta if t["role"] == "assistant" and "feedback" in (t.get("meta") or {})] == ["up", "down"]

    out = tmp_path / "export.jsonl"
    counts = training_log.export(str(log), str(out))
    assert counts == {"sft": 2, "preference": 1, "skipped": 0}
    records = [json.loads(l) for l in out.read_text().splitlines()]
    pref = next(r for r in records if r["kind"] == "preference")
    assert pref["chosen"].startswith("You're welcome") and pref["messages"][-1]["content"] == "thanks"
    rated = training_log.export(str(log), str(out), only_rated=True)
    assert rated == {"sft": 1, "preference": 1, "skipped": 1}


def test_other_users_cannot_rate(client, monkeypatch):
    from backend.auth import StaticAuthVerifier
    monkeypatch.setenv("TORA_AUTH_MODE", "optional")
    monkeypatch.setattr(main_module, "auth_verifier", StaticAuthVerifier({"ta": "alice", "tb": "bob"}))
    cid = client.post("/api/chat", json={"message": "hi"}, headers={"Authorization": "Bearer ta"}).json()["conversation_id"]
    r = client.post("/api/feedback", json={"conversation_id": cid, "turn": 1, "rating": "up"},
                    headers={"Authorization": "Bearer tb"})
    assert r.status_code == 404
    main_module.session_store.delete_user_data("alice")
