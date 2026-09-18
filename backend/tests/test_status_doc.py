"""The status doc is generated from the code, so the generator must stay truthful."""
import backend.main as main_module
from backend.finance.engine import OPERATIONS
from backend.status import collect, render


def test_status_reflects_the_running_system():
    data = collect()
    assert {t["name"] for t in data["tools"]} == {t.name for t in main_module.tool_registry.list_tools()}
    assert set(data["finance_ops"]) == set(OPERATIONS)
    paths = {p for _, p, _ in data["endpoints"]}
    # every route registered on the app is described, with a purpose line
    registered = {r.path for r in main_module.app.routes if getattr(r, "methods", None) and r.path.startswith(("/api", "/chat", "/"))}
    assert registered - paths - {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"} == set()
    assert all(purpose for _, path, purpose in data["endpoints"] if path != "/")
    assert "debt_rescue_plan" in data["finance_ops"] and "compare_regimes" in data["tax_ops"]
    assert "memory_delete" in data["intents"] and "research_followup" in data["intents"]
    assert data["rules"]["count"] > 0 and data["evals"]["scenarios"] > 100


def test_status_renders_without_test_counts():
    text = render(collect(), None, None)
    for heading in ("## Built", "## Partial", "## Open", "## Known limits", "### Safety", "### Documents"):
        assert heading in text
    assert "python -m backend.status" in text


class TestStatusDocCheck:
    """CI runs `python -m backend.status --check`. It must fail on real drift and must not
    fail merely because a day has passed (the first version of it did exactly that)."""

    def test_a_different_day_is_not_drift(self):
        from backend.status import _without_date
        a = _without_date("Generated from the code on 2026-09-18 by `python -m backend.status`.")
        b = _without_date("Generated from the code on 2027-01-02 by `python -m backend.status`.")
        assert a == b
        assert "2026" not in a

    def test_changed_content_is_drift(self, tmp_path):
        from backend.status import _check
        doc = tmp_path / "STATUS.md"
        doc.write_text("Generated from the code on 2026-09-18 by x.\n| Backend tests | 10 |\n",
                       encoding="utf-8")
        same = "Generated from the code on 2099-12-31 by x.\n| Backend tests | 10 |\n"
        changed = "Generated from the code on 2099-12-31 by x.\n| Backend tests | 11 |\n"
        assert _check(doc, same) == 0
        assert _check(doc, changed) == 1

    def test_a_missing_doc_is_drift(self, tmp_path):
        from backend.status import _check
        assert _check(tmp_path / "nope.md", "anything") == 1
