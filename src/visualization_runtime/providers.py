"""Bounded OpenAI-compatible calls, without retries or raw error disclosure."""

import asyncio
import json
import re
from dataclasses import dataclass, field

import httpx

from visualization_runtime.settings import ProviderSettings


class ProviderError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        unknown: bool = False,
        usage: dict | None = None,
    ):
        super().__init__(code)
        self.code, self.status, self.unknown = code, status, unknown
        self.usage = usage or {}


@dataclass(frozen=True)
class Completion:
    content: str = field(repr=False)
    model_matches: bool
    usage: dict
    request_id: str | None = None


def token_usage(value):
    raw = value.get("usage") if isinstance(value, dict) else None
    return {
        name: raw[name]
        for name in ("prompt_tokens", "completion_tokens", "total_tokens")
        if isinstance(raw, dict) and type(raw.get(name)) is int and raw[name] >= 0
    }


async def complete(
    config: ProviderSettings,
    messages: list[dict],
    *,
    max_tokens=1200,
    json_object=False,
    json_schema=None,
    transport=None,
) -> Completion:
    if type(max_tokens) is not int or not 1 <= max_tokens <= 8192:
        raise ValueError("invalid_output_limit")
    payload = {
        "model": config.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if config.role == "code":
        payload["thinking"] = {"type": "disabled"}
    elif config.model.startswith("qwen"):
        payload["enable_thinking"] = False
    if json_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "visualization_review", "strict": True, "schema": json_schema},
        }
    elif json_object:
        payload["response_format"] = {"type": "json_object"}
    try:
        async with (
            asyncio.timeout(config.timeout_seconds),
            httpx.AsyncClient(
                transport=transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(
                    config.timeout_seconds, connect=min(10, config.timeout_seconds)
                ),
            ) as client,
            client.stream(
                "POST",
                config.base_url + "/chat/completions",
                json=payload,
                headers={"Authorization": "Bearer " + config.api_key.get_secret_value()},
            ) as response,
        ):
            if response.status_code != 200:
                code = {
                    401: "authentication_failed",
                    403: "access_denied",
                    404: "endpoint_or_model_not_found",
                    429: "rate_limited",
                }.get(response.status_code, "provider_http_error")
                raise ProviderError(
                    code, status=response.status_code, unknown=response.status_code >= 500
                )
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 1_000_000:
                    raise ProviderError("response_too_large", unknown=True)
    except (TimeoutError, httpx.TimeoutException):
        raise ProviderError("provider_timeout", unknown=True) from None
    except httpx.TransportError:
        raise ProviderError("provider_transport_error", unknown=True) from None
    try:
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ProviderError("invalid_completion_envelope", unknown=True)
        usage = token_usage(value)
        choice = value["choices"][0]
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            raise ProviderError("invalid_completion_envelope", unknown=True, usage=usage)
        content = choice["message"]["content"]
        if choice.get("finish_reason") != "stop":
            raise ProviderError("incomplete_completion", usage=usage)
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("empty_completion", usage=usage)
        request_id = value.get("id")
        if not isinstance(request_id, str) or not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,160}", request_id
        ):
            request_id = None
        return Completion(content, value.get("model") == config.model, usage, request_id)
    except (ValueError, KeyError, IndexError, TypeError):
        raise ProviderError("invalid_completion_envelope", unknown=True) from None
