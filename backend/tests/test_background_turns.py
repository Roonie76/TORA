"""
Background turns: a slow answer must outlive the connection that asked for it.

`/api/chat/stream` ties the turn to the socket -- close it and minutes of decode
are thrown away. On 2 CPU cores that is not an edge case: a debt plan takes ~345s,
which is longer than a phone stays awake. These tests pin the promise that makes
/api/chat/async different: the work finishes, the answer is kept, and a client may
leave and come back without losing what it missed.
"""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from backend import turns
from backend.turns import DONE, ERROR, RUNNING, CANCELLED, TurnRegistry


@pytest.fixture
def registry():
    return TurnRegistry()


class TestTheAnswerBuffer:
    """A late joiner needs the state of the answer, not the history of its typing."""

    def test_tokens_are_coalesced_into_one_string(self, registry):
        turn = registry.create("user:a")
        for piece in ("Your ", "EMI ", "is ", "Rs 43,391"):
            registry.publish(turn, {"type": "token", "text": piece})
        assert turn.text == "Your EMI is Rs 43,391"
        # and none of them were kept as separate events
        assert turn.events == []

    def test_replace_resets_the_answer_rather_than_appending(self, registry):
        """
        The grounding path rewrites a whole answer. The UI replaces on this event;
        if the buffer appended instead, a client attaching afterwards would be shown
        the discarded draft followed by the corrected one.
        """
        turn = registry.create("user:a")
        registry.publish(turn, {"type": "token", "text": "rent is Rs 20,000"})
        registry.publish(turn, {"type": "replace", "text": "I don't have your rent on file.", "reason": "regenerate"})
        assert turn.text == "I don't have your rent on file."
        assert "20,000" not in turn.text

    def test_non_token_events_are_kept_in_order(self, registry):
        turn = registry.create("user:a")
        registry.publish(turn, {"type": "stage", "stage": "planning"})
        registry.publish(turn, {"type": "token", "text": "x"})
        registry.publish(turn, {"type": "tool", "name": "finance_calc"})
        assert [e["type"] for e in turn.events] == ["stage", "tool"]

    def test_the_answer_is_capped(self, registry, monkeypatch):
        monkeypatch.setattr(turns, "MAX_ANSWER_CHARS", 10)
        turn = registry.create("user:a")
        registry.publish(turn, {"type": "token", "text": "x" * 25})
        assert len(turn.text) == 10
        assert turn.truncated is True

    def test_runaway_events_are_counted_not_accumulated(self, registry, monkeypatch):
        monkeypatch.setattr(turns, "MAX_EVENTS", 3)
        turn = registry.create("user:a")
        for _ in range(10):
            registry.publish(turn, {"type": "tool", "name": "web_search"})
        assert len(turn.events) == 3
        assert turn.dropped_events == 7

    def test_the_saved_answer_wins_over_the_reconstructed_one(self, registry):
        """What the conversation stored is authoritative; tokens are only a preview."""
        turn = registry.create("user:a")
        registry.publish(turn, {"type": "token", "text": "partial..."})
        registry.finish(turn, {"response": "The complete, saved answer.", "model": "gemma4:e4b"})
        assert turn.text == "The complete, saved answer."
        assert turn.status == DONE


class TestOwnership:
    def test_a_turn_is_invisible_to_anyone_else(self, registry):
        turn = registry.create("user:alice")
        assert registry.get(turn.turn_id, "user:alice") is turn
        assert registry.get(turn.turn_id, "user:bob") is None

    def test_a_missing_turn_and_someone_elses_are_indistinguishable(self, registry):
        """Both return None, so a caller cannot probe for other people's turn ids."""
        turn = registry.create("user:alice")
        assert registry.get(turn.turn_id, "user:bob") is None
        assert registry.get("does-not-exist", "user:bob") is None


class TestRetention:
    def test_finished_turns_expire(self, registry, monkeypatch):
        monkeypatch.setattr(turns, "TURN_TTL_SECONDS", 0.0)
        turn = registry.create("user:a")
        registry.finish(turn, {"response": "done", "model": "m"})
        registry.create("user:a")  # any create sweeps
        assert registry.get(turn.turn_id, "user:a") is None

    def test_a_running_turn_is_never_evicted(self, registry, monkeypatch):
        """
        Evicting a running turn would throw away work in progress -- the exact
        failure this module exists to prevent -- so the ceiling must not touch it.
        """
        monkeypatch.setattr(turns, "MAX_TURNS", 2)
        running = registry.create("user:a")
        for _ in range(6):
            done = registry.create("user:a")
            registry.finish(done, {"response": "x", "model": "m"})
        assert registry.get(running.turn_id, "user:a") is running
        assert registry.stats()["running"] == 1


class TestCancellation:
    def test_cancel_marks_the_turn_and_reports_it(self, registry):
        turn = registry.create("user:a")
        assert registry.cancel(turn) is True
        assert turn.status == CANCELLED

    def test_cancelling_a_finished_turn_changes_nothing(self, registry):
        turn = registry.create("user:a")
        registry.finish(turn, {"response": "done", "model": "m"})
        assert registry.cancel(turn) is False
        assert turn.status == DONE


class TestListeners:
    @pytest.mark.asyncio
    async def test_a_listener_receives_live_events(self, registry):
        turn = registry.create("user:a")
        queue = registry.attach(turn)
        registry.publish(turn, {"type": "stage", "stage": "writing"})
        assert (await queue.get())["type"] == "stage"

    @pytest.mark.asyncio
    async def test_attaching_to_a_finished_turn_closes_immediately(self, registry):
        """No hang: the snapshot already carries everything, so there is nothing to wait for."""
        turn = registry.create("user:a")
        registry.finish(turn, {"response": "done", "model": "m"})
        queue = registry.attach(turn)
        assert await asyncio.wait_for(queue.get(), timeout=1) is None

    @pytest.mark.asyncio
    async def test_detaching_does_not_stop_the_turn(self, registry):
        turn = registry.create("user:a")
        queue = registry.attach(turn)
        registry.detach(turn, queue)
        registry.publish(turn, {"type": "token", "text": "still working"})
        assert turn.status == RUNNING
        assert turn.text == "still working"


# --------------------------------------------------------------------------
# The endpoints
# --------------------------------------------------------------------------


def _client(monkeypatch, answer="The answer.", delay=0.0, fail=None):
    """A TestClient whose turns complete without touching a model."""
    import backend.main as main

    async def fake_chat(request, http_request, identity=None, client_key=None):
        from backend.observability import progress

        progress.emit("stage", stage="writing")
        progress.emit("token", text=answer)
        if delay:
            await asyncio.sleep(delay)
        if fail is not None:
            raise fail
        return main.ChatResponse(response=answer, model="gemma4:e4b", conversation_id="conv-1")

    monkeypatch.setattr(main, "_chat", fake_chat)
    turns.registry.clear()
    return TestClient(main.app), main


def _await_turn(c, turn_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = c.get(f"/api/chat/turns/{turn_id}").json()
        if body["status"] != RUNNING:
            return body
        time.sleep(0.02)
    raise AssertionError(f"turn {turn_id} never finished")


class TestTheAsyncEndpoint:
    def test_it_accepts_and_returns_at_once(self, monkeypatch):
        c, _ = _client(monkeypatch)
        with c:
            r = c.post("/api/chat/async", json={"message": "What is my rent?"})
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == RUNNING
        assert body["poll"].endswith(body["turn_id"])

    def test_the_turn_completes_after_the_response_was_already_sent(self, monkeypatch):
        """The whole point: the work is not tied to the request that started it."""
        c, _ = _client(monkeypatch, answer="Rs 43,391 a month.", delay=0.05)
        with c:
            turn_id = c.post("/api/chat/async", json={"message": "EMI?"}).json()["turn_id"]
            body = _await_turn(c, turn_id)
        assert body["status"] == DONE
        assert body["result"]["response"] == "Rs 43,391 a month."
        assert body["text"] == "Rs 43,391 a month."

    def test_progress_is_readable_while_it_runs(self, monkeypatch):
        c, _ = _client(monkeypatch, answer="partial", delay=0.4)
        with c:
            turn_id = c.post("/api/chat/async", json={"message": "advice?"}).json()["turn_id"]
            time.sleep(0.15)
            mid = c.get(f"/api/chat/turns/{turn_id}").json()
            assert mid["status"] == RUNNING
            assert mid["text"] == "partial"
            assert any(e["type"] == "stage" for e in mid["events"])
            _await_turn(c, turn_id)

    def test_a_failing_turn_is_recorded_not_lost(self, monkeypatch):
        c, _ = _client(monkeypatch, fail=RuntimeError("engine exploded"))
        with c:
            turn_id = c.post("/api/chat/async", json={"message": "x"}).json()["turn_id"]
            body = _await_turn(c, turn_id)
        assert body["status"] == ERROR
        assert body["error"]["status"] == 500
        # The internal message never reaches the user.
        assert "exploded" not in body["error"]["detail"]

    def test_an_unknown_turn_is_a_404(self, monkeypatch):
        c, _ = _client(monkeypatch)
        with c:
            assert c.get("/api/chat/turns/nope").status_code == 404

    def test_a_turn_can_be_cancelled(self, monkeypatch):
        c, _ = _client(monkeypatch, delay=5.0)
        with c:
            turn_id = c.post("/api/chat/async", json={"message": "long one"}).json()["turn_id"]
            r = c.delete(f"/api/chat/turns/{turn_id}")
            assert r.json()["stopped"] is True
            assert r.json()["status"] == CANCELLED

    def test_the_event_stream_opens_with_a_snapshot(self, monkeypatch):
        """
        A client attaching late -- or re-attaching after its connection dropped --
        must be told everything it missed before live events resume.
        """
        c, _ = _client(monkeypatch, answer="Rs 11,492 a month.")
        with c:
            turn_id = c.post("/api/chat/async", json={"message": "SIP?"}).json()["turn_id"]
            _await_turn(c, turn_id)
            with c.stream("GET", f"/api/chat/turns/{turn_id}/events") as r:
                assert r.status_code == 200
                body = "".join(chunk for chunk in r.iter_text())
        assert "event: snapshot" in body
        assert "Rs 11,492 a month." in body
