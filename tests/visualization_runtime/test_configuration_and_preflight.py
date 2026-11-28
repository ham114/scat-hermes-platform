"""Independent credentials and actual-image probe behavior, without network calls."""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from visualization_runtime.preflight import fixture_png, run_preflight, vision_probe_valid
from visualization_runtime.providers import ProviderError, complete
from visualization_runtime.settings import ConfigurationError, VisualizationSettings


@pytest.fixture(autouse=True)
def clean_visualization_environment(monkeypatch):
    import os

    for key in os.environ:
        if key.startswith("SCAT_VIS_"):
            monkeypatch.delenv(key)


def configured(**overrides):
    return VisualizationSettings(
        code_api_key="test-code-secret",
        code_base_url="https://api.deepseek.com",
        review_api_key="test-review-secret",
        review_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        **overrides,
    )


def envelope(content, model="deepseek-flash", reason="stop"):
    return {
        "model": model,
        "choices": [{"finish_reason": reason, "message": {"content": content}}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
    }


def test_no_implicit_main_credential_fallback(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unrelated-main-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai-secret")
    settings = VisualizationSettings()
    assert settings.enabled is False
    with pytest.raises(ConfigurationError, match="SCAT_VIS_CODE_API_KEY"):
        settings.provider("code")
    assert "test-code-secret" not in repr(configured())
    assert "test-review-secret" not in repr(configured())
    assert "test-code-secret" not in repr(configured().provider("code"))


@pytest.mark.parametrize(
    "url",
    [
        "http://api.deepseek.com",
        "https://api.deepseek.com.evil.test",
        "https://user:secret@api.deepseek.com",
        "https://api.deepseek.com/chat/completions",
        "https://api.deepseek.com?api_key=secret",
        "https://127.0.0.1",
    ],
)
def test_invalid_endpoint_does_not_disclose_value(url):
    with pytest.raises(ValidationError) as error:
        VisualizationSettings(code_base_url=url)
    assert url not in str(error.value)


def test_template_is_disabled_and_empty_credentials_do_not_configure_provider():
    from pathlib import Path

    settings = VisualizationSettings(_env_file=Path(__file__).parents[2] / ".env.example")
    assert settings.enabled is False
    assert settings.code_api_key is None
    assert settings.review_api_key is None


@pytest.mark.parametrize("value", ["../secrets", "C:/private", "/private", ".", ""])
def test_artifact_root_is_confined(value):
    with pytest.raises(ValidationError):
        VisualizationSettings(artifact_root=value)


def test_dry_run_does_not_make_requests():
    def forbidden(request):
        raise AssertionError("dry run made a request")

    report = asyncio.run(run_preflight(configured(), transport=httpx.MockTransport(forbidden)))
    assert report["passed"]
    assert all(item["request_count"] == 0 for item in report["probes"])


def test_real_probe_wire_format_uses_two_separate_keys_and_image_bytes():
    requests = []

    def respond(request):
        requests.append(request)
        payload = json.loads(request.content)
        if request.url.host == "api.deepseek.com":
            assert request.headers["authorization"] == "Bearer test-code-secret"
            assert payload["thinking"] == {"type": "disabled"}
            content = "export async function render({echarts}) { echarts.init().setOption({}); }"
        else:
            assert request.headers["authorization"] == "Bearer test-review-secret"
            assert payload["response_format"] == {"type": "json_object"}
            assert payload["temperature"] == 0.1
            parts = payload["messages"][0]["content"]
            assert parts[1]["type"] == "image_url"
            assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,iVBOR")
            assert "blue" not in parts[0]["text"]
            content = '{"highest_label":"B","highest_color":"blue","bar_count":3}'
        return httpx.Response(200, json=envelope(content, payload["model"]))

    report = asyncio.run(
        run_preflight(configured(), live=True, transport=httpx.MockTransport(respond))
    )
    assert report["passed"] and len(requests) == 2
    assert report["feature_enabled"] is False
    assert "test-code-secret" not in json.dumps(report)
    assert "test-review-secret" not in json.dumps(report)
    assert all(item["usage"]["total_tokens"] == 7 for item in report["probes"])
    assert fixture_png().startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.parametrize(
    "answer",
    [
        "{}",
        "not json",
        '{"highest_label":"A","highest_color":"blue","bar_count":3}',
        '{"highest_label":"B","highest_color":"blue","bar_count":true}',
    ],
)
def test_incorrect_visual_answer_fails(answer):
    assert not vision_probe_valid(answer)


def test_http_failure_is_sanitized_and_not_retried():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(401, text="invalid key test-code-secret")

    report = asyncio.run(
        run_preflight(configured(), live=True, transport=httpx.MockTransport(respond))
    )
    assert not report["passed"] and len(calls) == 2
    assert all(r["error"] == "authentication_failed" for r in report["probes"])
    assert "secret" not in json.dumps(report)


@pytest.mark.parametrize(
    "response,expected",
    [
        (httpx.Response(302, headers={"location": "https://evil.test"}), "provider_http_error"),
        (httpx.Response(200, json=envelope("partial", reason="length")), "incomplete_completion"),
        (httpx.Response(200, content=b"x" * 1_000_001), "response_too_large"),
        (httpx.Response(200, json={}), "invalid_completion_envelope"),
    ],
)
def test_unsafe_or_incomplete_response_is_rejected(response, expected):
    calls = []

    def respond(request):
        calls.append(request)
        return response

    with pytest.raises(ProviderError, match=expected):
        asyncio.run(
            complete(configured().provider("code"), [], transport=httpx.MockTransport(respond))
        )
    assert len(calls) == 1


def test_timeout_reports_unknown_without_exception_text():
    def respond(request):
        raise httpx.ReadTimeout("secret internal URL", request=request)

    with pytest.raises(ProviderError) as error:
        asyncio.run(
            complete(configured().provider("review"), [], transport=httpx.MockTransport(respond))
        )
    assert error.value.unknown
    assert str(error.value) == "provider_timeout"


@pytest.mark.parametrize(
    "exception,code",
    [
        (httpx.ConnectTimeout, "provider_connection_timeout"),
        (httpx.ConnectError, "provider_connection_failed"),
        (httpx.PoolTimeout, "provider_pool_timeout"),
    ],
)
def test_connection_failure_is_distinct_from_unknown_sent_request(exception, code, caplog):
    calls = []

    def respond(request):
        calls.append(request)
        raise exception("secret provider details", request=request)

    with pytest.raises(ProviderError) as error:
        asyncio.run(
            complete(configured().provider("code"), [], transport=httpx.MockTransport(respond))
        )
    assert str(error.value) == code and not error.value.unknown
    assert len(calls) == 1
    assert "secret provider details" not in caplog.text


def test_write_timeout_remains_unknown_and_is_not_retried():
    calls = []

    def respond(request):
        calls.append(request)
        raise httpx.WriteTimeout("secret write details", request=request)

    with pytest.raises(ProviderError) as error:
        asyncio.run(
            complete(configured().provider("code"), [], transport=httpx.MockTransport(respond))
        )
    assert error.value.unknown and str(error.value) == "provider_timeout"
    assert len(calls) == 1


def test_total_deadline_limits_slow_provider():
    async def respond(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=envelope("late result"))

    with pytest.raises(ProviderError, match="provider_timeout"):
        asyncio.run(
            complete(
                configured(api_timeout_seconds=0.01).provider("code"),
                [],
                transport=httpx.MockTransport(respond),
            )
        )


def test_truncated_response_preserves_reported_usage():
    def respond(request):
        return httpx.Response(200, json=envelope("incomplete", reason="length"))

    report = asyncio.run(
        run_preflight(
            configured(), live=True, roles=("code",), transport=httpx.MockTransport(respond)
        )
    )
    assert len(report["probes"]) == 1
    assert report["probes"][0]["usage"]["total_tokens"] == 7
    assert report["probes"][0]["error"] == "incomplete_completion"


def test_review_only_probe_does_not_retest_code_provider():
    calls = []

    def respond(request):
        calls.append(request)
        assert request.url.host != "api.deepseek.com"
        return httpx.Response(401)

    report = asyncio.run(
        run_preflight(
            configured(), live=True, roles=("review",), transport=httpx.MockTransport(respond)
        )
    )
    assert len(calls) == 1 and len(report["probes"]) == 1


@pytest.mark.parametrize("value", [[], {"choices": [None]}, {"choices": [{"message": None}]}])
def test_malformed_nested_envelope_is_sanitized(value):
    with pytest.raises(ProviderError, match="invalid_completion_envelope"):
        asyncio.run(
            complete(
                configured().provider("code"),
                [],
                transport=httpx.MockTransport(lambda request: httpx.Response(200, json=value)),
            )
        )


@pytest.mark.parametrize("save_report", [False, True])
def test_short_preflight_report_only_creates_a_file_when_requested(
    monkeypatch, tmp_path, capsys, save_report
):
    import importlib.util
    from pathlib import Path

    import visualization_runtime.settings as settings_module

    path = Path(__file__).parents[2] / "scripts/check_visualization_apis.py"
    spec = importlib.util.spec_from_file_location("visualization_api_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = tmp_path
    monkeypatch.setattr(settings_module, "load_settings", lambda root: configured())
    monkeypatch.setattr("sys.argv", [str(path)] + (["--save-report"] if save_report else []))
    assert module.main() == 0
    output = capsys.readouterr().out
    assert "test-code-secret" not in output and '"live": false' in output
    assert len(list(tmp_path.glob("output/**/*.json"))) == int(save_report)
