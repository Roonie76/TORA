"""The operations dashboard: one page over the metrics and traces that already exist.

It is safe to leave open on a screen, which is only true if it carries no question text, no
figures and nothing about any user — so that is what the tests check hardest.
"""
import pytest
from fastapi.testclient import TestClient


def client(monkeypatch, debug="1"):
    monkeypatch.setenv("TORA_DEBUG_ENDPOINTS", debug)
    import importlib

    import backend.main as main
    importlib.reload(main)
    return TestClient(main.app), main


class TestTheDashboardPage:
    def test_it_is_served_when_debug_endpoints_are_on(self, monkeypatch):
        c, _ = client(monkeypatch, "1")
        with c:
            r = c.get("/api/dashboard")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "TORA operations" in r.text

    def test_it_is_absent_in_production(self, monkeypatch):
        c, _ = client(monkeypatch, "0")
        with c:
            assert c.get("/api/dashboard").status_code == 404

    def test_it_is_self_contained(self, monkeypatch):
        """No external scripts or styles: it must render on a machine with no internet."""
        c, _ = client(monkeypatch, "1")
        with c:
            html = c.get("/api/dashboard").text
        assert "src=\"http" not in html and "href=\"http" not in html
        assert "cdn" not in html.lower()

    def test_it_fetches_absolute_paths(self, monkeypatch):
        """The page is served at /api/dashboard, so a relative "api/metrics" resolves to
        /api/api/metrics and every number silently reads zero. A screenshot caught this; a
        substring check for "api/metrics" did not."""
        c, _ = client(monkeypatch, "1")
        with c:
            html = c.get("/api/dashboard").text
        assert 'fetch("/api/metrics")' in html
        assert 'fetch("/api/traces' in html
        assert 'fetch("api/' not in html

    def test_the_timestamp_survives_microseconds(self, monkeypatch):
        """The backend writes 6 fractional digits; Date() does not always parse them, and the
        page showed "since Invalid Date"."""
        c, _ = client(monkeypatch, "1")
        with c:
            html = c.get("/api/dashboard").text
        assert "isNaN(d)" in html


class TestItLeaksNothing:
    def test_metrics_carry_no_content(self, monkeypatch):
        c, main = client(monkeypatch, "1")
        with c:
            body = c.get("/api/metrics").json()
        flat = repr(body).lower()
        for leak in ("salary", "rent", "₹", "message", "question", "answer", "response"):
            assert leak not in flat, f"metrics should not carry {leak!r}"

    def test_the_page_never_renders_a_field_that_could_hold_content(self, monkeypatch):
        c, _ = client(monkeypatch, "1")
        with c:
            html = c.get("/api/dashboard").text
        for field in ("tr.message", "tr.answer", "tr.response", "tr.question", ".content"):
            assert field not in html


def test_no_model_turns_is_counted(monkeypatch):
    """The headline number for the tier-0 work: turns finished without calling a model."""
    from backend.observability.trace import TelemetryHub, TurnTrace

    hub = TelemetryHub()
    engine_turn = TurnTrace()
    engine_turn.intent = "calculation"
    hub.finish(engine_turn)                       # no llm calls recorded
    model_turn = TurnTrace()
    model_turn.intent = "financial_qa"
    model_turn.llm["calls"] = 2
    hub.finish(model_turn)

    snap = hub.snapshot()
    assert snap["no_model_turns"] == 1
    assert snap["requests"]["total"] == 2


def test_engine_only_means_no_model_ran(monkeypatch):
    """A tier-0 answer can still have needed a planner call. Labelling that turn "engine only"
    on a 221-second row (seen in a screenshot) makes the whole page untrustworthy."""
    c, _ = client(monkeypatch, "1")
    with c:
        html = c.get("/api/dashboard").text
    assert 'calls === 0 ? `<span class="pill good">engine only</span>`' in html
    assert "planner call" in html
