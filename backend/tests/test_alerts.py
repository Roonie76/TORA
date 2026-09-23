"""
Alerting.

Detection is the easy half. These tests spend most of their effort on the half
that decides whether anyone still reads the alerts in a month: **not firing when
nothing is wrong.** An alert that goes off on a quiet system gets muted, and then
the real one is missed too.
"""

import logging

import pytest
from fastapi.testclient import TestClient

from backend.observability import alerts as alerting
from backend.observability.alerts import CRITICAL, WARNING, Alert, AlertLog, evaluate


def metrics(**over):
    base = {
        "requests": {"total": 0},
        "tools": {},
        "grounding": {},
    }
    base.update(over)
    return base


def keys(alerts):
    return [a.key for a in alerts]


class TestItStaysQuietWhenNothingIsWrong:
    def test_a_fresh_process_raises_nothing(self):
        assert evaluate(metrics()) == []

    def test_a_healthy_system_raises_nothing(self):
        out = evaluate(metrics(
            requests={"total": 500, "ok": 500},
            tools={"finance_calc": {"ok": 300, "error": 0}},
            grounding={"none": 400, "annotate": 5},
        ))
        assert out == []

    def test_one_failure_out_of_one_is_not_a_failing_tool(self):
        """
        The classic false alarm: a 100% error rate over a single call. Real
        enough to look alarming, far too little to act on.
        """
        out = evaluate(metrics(tools={"web_search": {"ok": 0, "error": 1}}))
        assert out == []

    def test_a_couple_of_failed_turns_on_a_quiet_system_is_not_an_outage(self):
        out = evaluate(metrics(requests={"total": 4, "ok": 2, "error": 2}))
        assert out == []

    def test_slow_answers_are_never_alerted_on(self):
        """
        On two cores a Tier 2 answer takes minutes by design. A latency rule
        would fire constantly and correctly mean nothing, so there isn't one --
        pinned here so nobody adds one back without reading why.
        """
        out = evaluate(metrics(
            requests={"total": 100, "ok": 100},
            latency_ms={"p50": 180000, "p95": 345000, "max": 400000, "samples": 100},
        ))
        assert out == []


class TestWhatItDoesFireOn:
    def test_a_tool_held_open_is_critical(self):
        out = evaluate(metrics(failing_tools={"tax_calc": {"open": True, "consecutive_failures": 3}}))
        assert keys(out) == ["tool_open:tax_calc"]
        assert out[0].severity == CRITICAL
        assert "tax_calc" in out[0].summary

    def test_a_tool_failing_often_but_not_yet_open(self):
        out = evaluate(metrics(tools={"web_search": {"ok": 2, "error": 8}}))
        assert keys(out) == ["tool_errors:web_search"]
        assert out[0].severity == WARNING
        assert out[0].detail["rate"] == 0.8

    def test_a_tool_failing_every_call_is_critical(self):
        out = evaluate(metrics(tools={"web_search": {"ok": 0, "error": 9}}))
        assert out[0].severity == CRITICAL

    def test_an_open_tool_is_not_also_reported_as_merely_failing(self):
        """One fault, one alert. Two lines for one broken tool is how noise starts."""
        out = evaluate(metrics(
            tools={"tax_calc": {"ok": 0, "error": 8}},
            failing_tools={"tax_calc": {"open": True, "consecutive_failures": 3}},
        ))
        assert keys(out) == ["tool_open:tax_calc"]

    def test_turns_failing_overall(self):
        out = evaluate(metrics(requests={"total": 40, "ok": 30, "error": 10}))
        assert keys(out) == ["request_errors"]
        assert out[0].severity == WARNING

    def test_most_turns_failing_is_critical(self):
        out = evaluate(metrics(requests={"total": 40, "ok": 15, "error": 25}))
        assert out[0].severity == CRITICAL

    def test_the_model_inventing_figures_is_worth_waking_up_for(self):
        """
        The one rule about answers being *wrong* rather than the service being
        down. Every regenerate means a figure no engine supports got written.
        """
        out = evaluate(metrics(grounding={"none": 20, "regenerate": 10}))
        assert keys(out) == ["grounding_regenerate"]
        assert "inventing numbers" in out[0].summary

    def test_a_few_rewrites_are_normal(self):
        out = evaluate(metrics(grounding={"none": 95, "regenerate": 5}))
        assert out == []

    def test_critical_faults_sort_first(self):
        out = evaluate(metrics(
            requests={"total": 40, "ok": 30, "error": 10},
            failing_tools={"tax_calc": {"open": True, "consecutive_failures": 3}},
        ))
        assert out[0].severity == CRITICAL
        assert [a.severity for a in out] == sorted([a.severity for a in out], key=lambda s: s != CRITICAL)


class TestTheLogDoesNotRepeatItself:
    def test_a_fault_is_logged_once_not_on_every_check(self, caplog):
        log = AlertLog()
        alert = Alert(key="tool_open:x", severity=CRITICAL, summary="x is open")
        with caplog.at_level(logging.WARNING, logger="tora.alerts"):
            log.update([alert])
            log.update([alert])
            log.update([alert])
        fired = [r for r in caplog.records if "x is open" in r.getMessage()]
        assert len(fired) == 1

    def test_it_says_when_a_fault_clears(self, caplog):
        log = AlertLog()
        alert = Alert(key="tool_open:x", severity=CRITICAL, summary="x is open")
        log.update([alert])
        with caplog.at_level(logging.INFO, logger="tora.alerts"):
            result = log.update([])
        assert keys(result["cleared"]) == ["tool_open:x"]
        assert log.firing == []

    def test_a_fault_that_returns_is_logged_again(self):
        log = AlertLog()
        alert = Alert(key="tool_open:x", severity=CRITICAL, summary="x is open")
        log.update([alert])
        log.update([])
        assert keys(log.update([alert])["new"]) == ["tool_open:x"]

    def test_critical_logs_at_error_and_warning_at_warning(self, caplog):
        log = AlertLog()
        with caplog.at_level(logging.WARNING, logger="tora.alerts"):
            log.update([
                Alert(key="a", severity=CRITICAL, summary="critical one"),
                Alert(key="b", severity=WARNING, summary="warning one"),
            ])
        levels = {r.levelno for r in caplog.records}
        assert logging.ERROR in levels and logging.WARNING in levels


@pytest.fixture
def clean_telemetry():
    """
    The endpoint reads process-global telemetry, which every other test that
    touches an endpoint also writes to. Without this, a full-suite run could
    accumulate enough failed turns to fire a real alert inside a test asserting
    silence -- a flake that would look like an alerting bug and is not one.
    """
    import backend.main as main

    main.telemetry.reset()
    alerting.log.reset()
    yield
    main.telemetry.reset()
    alerting.log.reset()


@pytest.mark.usefixtures("clean_telemetry")
class TestTheEndpoint:
    def test_it_reports_ok_on_a_healthy_system(self):
        import backend.main as main

        with TestClient(main.app) as c:
            body = c.get("/api/alerts").json()
        assert body["status"] == "ok"
        assert body["count"] == 0
        assert body["alerts"] == []

    def test_it_surfaces_a_broken_tool(self, monkeypatch):
        import backend.main as main

        monkeypatch.setattr(main.tool_executor.breaker, "snapshot",
                            lambda: {"tax_calc": {"open": True, "consecutive_failures": 3}})
        with TestClient(main.app) as c:
            body = c.get("/api/alerts").json()
        assert body["status"] == CRITICAL
        assert body["alerts"][0]["key"] == "tool_open:tax_calc"

    def test_it_is_level_triggered_so_it_cannot_latch(self, monkeypatch):
        """
        Reading the current state each time means a rule can never get stuck
        firing after the fault has gone.
        """
        import backend.main as main

        broken = {"tax_calc": {"open": True, "consecutive_failures": 3}}
        state = {"snap": broken}
        monkeypatch.setattr(main.tool_executor.breaker, "snapshot", lambda: state["snap"])
        with TestClient(main.app) as c:
            assert c.get("/api/alerts").json()["status"] == CRITICAL
            state["snap"] = {}
            assert c.get("/api/alerts").json()["status"] == "ok"

    def test_it_carries_no_user_content(self, monkeypatch):
        """Same rule as the dashboard: safe to leave open on a screen."""
        import backend.main as main

        monkeypatch.setattr(main.tool_executor.breaker, "snapshot",
                            lambda: {"spendsy_data": {"open": True, "consecutive_failures": 4}})
        with TestClient(main.app) as c:
            raw = c.get("/api/alerts").text
        for leak in ("₹", "rent", "salary", "message", "conversation_id"):
            assert leak not in raw
