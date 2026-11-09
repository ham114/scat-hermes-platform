"""Check DeepSeek configuration and wire requests without network or real keys."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from core.llm import get_model
from core.settings import Settings, settings
from schema.models import DeepseekModelName


@pytest.fixture(autouse=True)
def clear_model_cache():
    get_model.cache_clear()
    yield
    get_model.cache_clear()


def test_default_template_leaves_key_empty_and_selects_deepseek():
    template = Path(__file__).resolve().parents[2] / ".env.example"
    with patch.dict(os.environ, {}, clear=True):
        configured = Settings(_env_file=template)
    assert configured.DEFAULT_MODEL == DeepseekModelName.DEEPSEEK_FLASH
    assert configured.DEEPSEEK_API_KEY is None
    assert configured.DEEPSEEK_BASE_URL.rstrip("/") == "https://api.deepseek.com"
    assert configured.AVAILABLE_MODELS == set()
    assert configured.HOST == "127.0.0.1"
    assert configured.PORT == 18080


def test_deepseek_key_alone_selects_default_without_other_providers():
    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "unit-test-deepseek"}, clear=True):
        configured = Settings(_env_file=None)
    assert configured.DEFAULT_MODEL == DeepseekModelName.DEEPSEEK_FLASH
    assert configured.AVAILABLE_MODELS == set(DeepseekModelName)


def test_missing_deepseek_key_does_not_fall_back_to_openai_key(monkeypatch):
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", None)
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-other-provider")
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY must be configured"):
        get_model(DeepseekModelName.DEEPSEEK_FLASH)


@pytest.mark.parametrize(
    "model_name", [DeepseekModelName.DEEPSEEK_FLASH, DeepseekModelName.DEEPSEEK_V4_PRO]
)
def test_deepseek_tool_request_uses_own_key_endpoint_and_non_thinking_body(
    monkeypatch, model_name
):
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", SecretStr("unit-test-deepseek"))
    monkeypatch.setattr(settings, "DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    captured = []

    def respond(request):
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "id": "unit-chat",
                "object": "chat.completion",
                "created": 0,
                "model": model_name.value,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "synthetic result"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    tool = {
        "type": "function",
        "function": {
            "name": "search_scat_flights",
            "description": "Search synthetic flights",
            "parameters": {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
                "required": ["limit"],
            },
        },
    }
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with patch("core.llm.ChatOpenAI", side_effect=lambda **kw: ChatOpenAI(http_client=client, **kw)):
            model = get_model(model_name)
        result = model.bind_tools([tool]).invoke("synthetic question", stream=False)

    assert result.content == "synthetic result"
    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == "https://api.deepseek.com/chat/completions"
    assert request.headers["authorization"] == "Bearer unit-test-deepseek"
    payload = json.loads(request.content)
    assert payload["model"] == model_name.value
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["tools"][0]["function"]["name"] == "search_scat_flights"
