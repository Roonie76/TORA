"""Re-audit tests for ContextBuilder tool-output handling."""
from backend.context import ConversationContext, ContextBuilder, ToolContext, TokenBudgetManager
from backend.context.token_budget import estimate_messages_tokens


def _fetch_ctx(content: str) -> ToolContext:
    return ToolContext().add_result(
        tool_name="web_fetch",
        call_id="c1",
        output={"url": "https://a.example", "final_url": "https://a.example", "domain": "a.example",
                "title": "T", "content": content, "truncated": False},
    )


def test_external_data_counts_against_token_budget():
    budget = TokenBudgetManager(num_ctx=2048, max_generation_tokens=512, safety_margin_tokens=64)
    builder = ContextBuilder(default_system_prompt="You are TORA.", token_budget_manager=budget)
    history = []
    for i in range(40):
        history.append({"role": "user", "content": f"question {i} " + "x" * 200})
        history.append({"role": "assistant", "content": f"answer {i} " + "y" * 200})
    messages = builder.build(
        current_message="summarize",
        context=ConversationContext.from_list(history),
        tool_context=_fetch_ctx("z" * 2500),
    )
    assert messages[-2]["content"].startswith("<external_data")
    assert estimate_messages_tokens(messages) <= budget.max_prompt_budget


def test_non_dict_tool_output_does_not_crash_renderer():
    ctx = ToolContext().add_result(tool_name="calculator", call_id="c", output=1666.67)
    messages = ContextBuilder(default_system_prompt="S").build(current_message="q", tool_context=ctx)
    assert "Result = 1666.67" in messages[0]["content"]
    assert len(messages) == 2


def test_calculator_only_has_no_external_message():
    ctx = ToolContext().add_result(tool_name="calculator", call_id="c", output={"result": 5})
    messages = ContextBuilder(default_system_prompt="S").build(current_message="q", tool_context=ctx)
    assert [m["role"] for m in messages] == ["system", "user"]


def test_failed_web_call_stays_in_system_without_data_message():
    ctx = ToolContext().add_result(tool_name="web_search", call_id="c", output="search failed", is_error=True)
    messages = ContextBuilder(default_system_prompt="S").build(current_message="q", tool_context=ctx)
    assert "Error = search failed" in messages[0]["content"]
    assert [m["role"] for m in messages] == ["system", "user"]


def test_explicit_long_summary_is_clamped_to_budget():
    budget = TokenBudgetManager(num_ctx=1024, max_generation_tokens=256, safety_margin_tokens=32)
    builder = ContextBuilder(default_system_prompt="You are TORA.", token_budget_manager=budget)
    messages = builder.build(
        current_message="hi",
        context=ConversationContext.from_list([{"role": "user", "content": "a" * 300}]),
        summary="## Conversation History Summary\n" + ("- line of old context\n" * 400),
    )
    assert estimate_messages_tokens(messages) <= budget.max_prompt_budget


def test_budget_holds_across_many_shapes():
    import random

    rng = random.Random(7)
    budget = TokenBudgetManager(num_ctx=4096, max_generation_tokens=1024, safety_margin_tokens=128)
    builder = ContextBuilder(default_system_prompt="You are TORA. " * 20, token_budget_manager=budget)
    for _ in range(60):
        history = []
        for i in range(rng.randint(0, 60)):
            role = "user" if i % 2 == 0 else "assistant"
            history.append({"role": role, "content": f"turn {i} ₹{rng.randint(1, 99)}k " + "w" * rng.randint(1, 900)})
        tool_ctx = _fetch_ctx("p" * rng.randint(0, 6000)) if rng.random() < 0.5 else None
        messages = builder.build(
            current_message="q" * rng.randint(1, 400),
            context=ConversationContext.from_list(history) if history else None,
            tool_context=tool_ctx,
        )
        assert estimate_messages_tokens(messages) <= budget.max_prompt_budget
