import unittest
from unittest.mock import AsyncMock, MagicMock
import httpx

from backend.llm.base import (
    LLMProvider,
    LLMResponse,
    LLMConnectionError,
    LLMTimeoutError,
    LLMResponseError,
    LLMProviderError,
)
from backend.llm.ollama import OllamaProvider, normalize_ollama_host


# ---------------------------------------------------------------------------
# Host normalization
# ---------------------------------------------------------------------------

class TestHostNormalization(unittest.TestCase):
    def test_normalizes_empty_and_loopback(self):
        self.assertEqual(normalize_ollama_host(""), "http://127.0.0.1:11434")
        self.assertEqual(normalize_ollama_host("0.0.0.0"), "http://127.0.0.1:11434")
        self.assertEqual(normalize_ollama_host("0.0.0.0:11434"), "http://127.0.0.1:11434")
        self.assertEqual(normalize_ollama_host("127.0.0.1"), "http://127.0.0.1:11434")
        self.assertEqual(normalize_ollama_host("localhost"), "http://127.0.0.1:11434")

    def test_prepends_http_scheme(self):
        self.assertEqual(normalize_ollama_host("remote-host:11434"), "http://remote-host:11434")
        self.assertEqual(normalize_ollama_host("https://secure-ollama.internal"), "https://secure-ollama.internal")

    def test_preserves_none_as_default(self):
        self.assertEqual(normalize_ollama_host(None), "http://127.0.0.1:11434")


# ---------------------------------------------------------------------------
# resolve_model()
# ---------------------------------------------------------------------------

class TestResolveModel(unittest.TestCase):
    def setUp(self):
        self.provider = OllamaProvider(
            host="http://127.0.0.1:11434",
            default_model="gemma4:e4b",
            client=MagicMock(),  # not used for resolve_model
        )

    def test_exact_match_returns_requested_model(self):
        result = self.provider.resolve_model("llama3.2:3b", ["gemma4:e4b", "llama3.2:3b"])
        self.assertEqual(result, "llama3.2:3b")

    def test_no_requested_model_returns_default(self):
        result = self.provider.resolve_model(None, ["gemma4:e4b", "llama3.2:3b"])
        self.assertEqual(result, "gemma4:e4b")

    def test_fuzzy_match_ignores_punctuation(self):
        # "gemma4e4b" should fuzzy-match "gemma4:e4b"
        result = self.provider.resolve_model("gemma4e4b", ["llama3.2:3b", "gemma4:e4b"])
        self.assertEqual(result, "gemma4:e4b")

    def test_fallback_to_first_available_when_no_match(self):
        result = self.provider.resolve_model("nonexistent-model", ["gemma4:e4b", "llama3.2:3b"])
        self.assertEqual(result, "gemma4:e4b")

    def test_empty_available_list_returns_target_as_is(self):
        result = self.provider.resolve_model("gemma4:e4b", [])
        self.assertEqual(result, "gemma4:e4b")

    def test_default_model_used_when_no_request_and_empty_list(self):
        result = self.provider.resolve_model(None, [])
        self.assertEqual(result, "gemma4:e4b")


# ---------------------------------------------------------------------------
# OllamaProvider.generate()
# ---------------------------------------------------------------------------

class TestOllamaProvider(unittest.IsolatedAsyncioTestCase):
    def _make_provider(self, mock_client, default_model="gemma4:e4b"):
        mock_client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"models": [{"name": default_model}]}
        )
        return OllamaProvider(
            host="http://127.0.0.1:11434",
            default_model=default_model,
            client=mock_client,
        )

    async def test_generate_success(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "model": "gemma4:e4b",
                "message": {"role": "assistant", "content": "3-6 months of expenses."},
                "done": True,
            },
        )

        provider = self._make_provider(mock_client)
        messages = [
            {"role": "system", "content": "You are TORA."},
            {"role": "user", "content": "How much for emergency fund?"},
        ]

        result = await provider.generate(messages=messages)

        self.assertIsInstance(result, LLMResponse)
        self.assertEqual(result.content, "3-6 months of expenses.")
        self.assertEqual(result.model, "gemma4:e4b")
        self.assertTrue(result.done)

        # Verify POST payload sent to Ollama
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        self.assertEqual(call_args[0][0], "http://127.0.0.1:11434/api/chat")
        payload = call_args[1]["json"]
        self.assertEqual(payload["model"], "gemma4:e4b")
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["options"]["temperature"], 0.7)
        self.assertEqual(payload["options"]["num_ctx"], 8192)
        self.assertEqual(payload["messages"], messages)

    async def test_custom_model_and_options_propagate(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "model": "llama3.2:3b",
                "message": {"role": "assistant", "content": "Custom model response"},
                "done": True,
            },
        )
        mock_client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"models": [{"name": "gemma4:e4b"}, {"name": "llama3.2:3b"}]}
        )

        provider = OllamaProvider(
            host="http://127.0.0.1:11434",
            default_model="gemma4:e4b",
            client=mock_client,
        )

        await provider.generate(
            messages=[{"role": "user", "content": "Hi"}],
            model="llama3.2:3b",
            options={"temperature": 0.2, "num_ctx": 4096},
        )

        payload = mock_client.post.call_args[1]["json"]
        self.assertEqual(payload["model"], "llama3.2:3b")
        self.assertEqual(payload["options"]["temperature"], 0.2)
        self.assertEqual(payload["options"]["num_ctx"], 4096)

    async def test_ollama_non_200_raises_llm_response_error(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = MagicMock(
            status_code=404,
            text="model 'nonexistent' not found",
        )

        provider = self._make_provider(mock_client)

        with self.assertRaises(LLMResponseError) as ctx:
            await provider.generate(messages=[{"role": "user", "content": "test"}])

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("not found", ctx.exception.detail)

    async def test_connection_error_raises_llm_connection_error(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, json=lambda: {"models": []})
        mock_client.post.side_effect = httpx.ConnectError("Connection refused")

        provider = OllamaProvider(
            host="http://127.0.0.1:11434",
            default_model="gemma4:e4b",
            client=mock_client,
        )

        with self.assertRaises(LLMConnectionError) as ctx:
            await provider.generate(messages=[{"role": "user", "content": "test"}])

        self.assertIn("Could not connect to Ollama", str(ctx.exception))

    async def test_timeout_error_raises_llm_timeout_error(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, json=lambda: {"models": []})
        mock_client.post.side_effect = httpx.TimeoutException("Timed out")

        provider = OllamaProvider(
            host="http://127.0.0.1:11434",
            default_model="gemma4:e4b",
            client=mock_client,
        )

        with self.assertRaises(LLMTimeoutError):
            await provider.generate(messages=[{"role": "user", "content": "test"}])

    async def test_unexpected_exception_raises_llm_provider_error(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(status_code=200, json=lambda: {"models": []})
        mock_client.post.side_effect = RuntimeError("Something unexpected")

        provider = OllamaProvider(
            host="http://127.0.0.1:11434",
            default_model="gemma4:e4b",
            client=mock_client,
        )

        with self.assertRaises(LLMProviderError) as ctx:
            await provider.generate(messages=[{"role": "user", "content": "test"}])

        self.assertIn("Something unexpected", str(ctx.exception))


# ---------------------------------------------------------------------------
# list_models() and health_check()
# ---------------------------------------------------------------------------

class TestModelDiscoveryAndHealth(unittest.IsolatedAsyncioTestCase):
    async def test_list_models_success(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "models": [
                    {"name": "gemma4:e4b", "size": 9600000000},
                    {"name": "llama3.2:3b", "size": 2000000000},
                ]
            },
        )

        provider = OllamaProvider(
            host="http://127.0.0.1:11434",
            default_model="gemma4:e4b",
            client=mock_client,
        )

        models = await provider.list_models()
        self.assertEqual(models, ["gemma4:e4b", "llama3.2:3b"])

    async def test_list_models_returns_empty_on_connection_failure(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.side_effect = httpx.ConnectError("Ollama is down")

        provider = OllamaProvider(
            host="http://127.0.0.1:11434",
            default_model="gemma4:e4b",
            client=mock_client,
        )

        models = await provider.list_models()
        self.assertEqual(models, [])

    async def test_health_check_success(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"models": [{"name": "gemma4:e4b"}]},
        )

        provider = OllamaProvider(
            host="http://127.0.0.1:11434",
            default_model="gemma4:e4b",
            client=mock_client,
        )

        health = await provider.health_check()
        self.assertTrue(health["connected"])
        self.assertEqual(health["models"], ["gemma4:e4b"])
        self.assertEqual(health["default_model"], "gemma4:e4b")
        self.assertEqual(health["host"], "http://127.0.0.1:11434")

    async def test_health_check_returns_disconnected_on_failure(self):
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.side_effect = httpx.ConnectError("Ollama is down")

        provider = OllamaProvider(
            host="http://127.0.0.1:11434",
            default_model="gemma4:e4b",
            client=mock_client,
        )

        health = await provider.health_check()
        self.assertFalse(health["connected"])
        self.assertEqual(health["models"], [])


# ---------------------------------------------------------------------------
# FastAPI route tests (only routes NOT tested in test_agent.py)
# ---------------------------------------------------------------------------

class TestFastAPIRoutes(unittest.TestCase):
    """Tests for /api/models and /api/health routes only.
    Chat route tests live in test_agent.py to avoid redundancy."""

    def setUp(self):
        from fastapi.testclient import TestClient
        from backend.main import app, agent
        self.client = TestClient(app)
        self.agent = agent

    def test_api_models_endpoint(self):
        with unittest.mock.patch.object(
            self.agent.llm_provider, "list_models", new_callable=AsyncMock
        ) as mock_list:
            mock_list.return_value = ["gemma4:e4b"]

            response = self.client.get("/api/models")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["models"], ["gemma4:e4b"])
            self.assertEqual(data["default"], "gemma4:e4b")

    def test_api_health_endpoint(self):
        with unittest.mock.patch.object(
            self.agent.llm_provider, "health_check", new_callable=AsyncMock
        ) as mock_health:
            mock_health.return_value = {
                "host": "http://127.0.0.1:11434",
                "connected": True,
                "models": ["gemma4:e4b"],
                "default_model": "gemma4:e4b",
            }

            response = self.client.get("/api/health")
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["status"], "ok")
            self.assertTrue(data["ollama"]["connected"])


if __name__ == "__main__":
    unittest.main()
