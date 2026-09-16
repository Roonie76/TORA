import unittest
import pytest
from unittest.mock import AsyncMock, patch
from backend.agent.agent import ToraAgent, AgentResponse
from backend.llm.base import LLMProvider, LLMResponse, LLMResponseError
from backend.context.conversation import ConversationContext, ConversationMessage
from backend.context.financial import FinancialProfile


class MockLLMProvider(LLMProvider):
    def __init__(self):
        self._default_model = "mock-model"
        self.call_count = 0
        self.should_fail_first_with_length = False
        self.should_always_fail_with_length = False

    @property
    def default_model(self) -> str:
        return self._default_model

    def resolve_model(self, requested=None, available=None):
        return requested or self._default_model

    async def generate(self, messages, model=None, options=None):
        self.call_count += 1
        if self.should_always_fail_with_length:
            raise LLMResponseError(
                message="LLM response was truncated due to context length limits.",
                status_code=None,
                detail="done_reason=length",
            )
        if self.should_fail_first_with_length and self.call_count == 1:
            raise LLMResponseError(
                message="LLM response was truncated due to context length limits.",
                status_code=None,
                detail="done_reason=length",
            )
        return LLMResponse(
            content="Recovered answer from TORA",
            model="mock-model",
            done=True,
        )

    async def list_models(self):
        return ["mock-model"]

    async def health_check(self):
        return {"connected": True}


class TestContextOverflowRecovery(unittest.IsolatedAsyncioTestCase):

    async def test_successful_one_shot_recovery(self):
        mock_provider = MockLLMProvider()
        mock_provider.should_fail_first_with_length = True

        agent = ToraAgent(llm_provider=mock_provider, planner=None, tool_executor=None)

        # Build long history
        history = [
            ConversationMessage(role="user", content=f"Turn {i} " + "long text " * 50)
            for i in range(10)
        ]
        ctx = ConversationContext(messages=history)

        response = await agent.run(
            message="Final question",
            context=ctx,
        )

        # Provider should have been called twice (first attempt failed with length, second attempt recovered)
        self.assertEqual(mock_provider.call_count, 2)
        self.assertEqual(response.content, "Recovered answer from TORA")

    async def test_failure_propagates_if_retry_fails_cleanly(self):
        mock_provider = MockLLMProvider()
        mock_provider.should_always_fail_with_length = True

        agent = ToraAgent(llm_provider=mock_provider, planner=None, tool_executor=None)
        ctx = ConversationContext(messages=[ConversationMessage(role="user", content="Turn 1")])

        with self.assertRaises(LLMResponseError) as cm:
            await agent.run(message="Question", context=ctx)

        # Provider was called twice (original + 1 retry), then cleanly raised LLMResponseError
        self.assertEqual(mock_provider.call_count, 2)
        self.assertIn("done_reason=length", cm.exception.detail)


if __name__ == "__main__":
    unittest.main()
