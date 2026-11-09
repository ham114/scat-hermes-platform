from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from schema.models import FakeModelName, OpenAICompatibleName
from service.runtime_validation import (
    validate_runtime_configuration,
    validate_scat_configuration,
)


def _valid_settings() -> SimpleNamespace:
    return SimpleNamespace(
        DEFAULT_MODEL=FakeModelName.FAKE,
        AVAILABLE_MODELS={FakeModelName.FAKE},
        COMPATIBLE_BASE_URL=None,
        COMPATIBLE_MODEL=None,
        COMPATIBLE_API_KEY=None,
        DEEPSEEK_API_KEY=None,
        FLIGHT_MONGO_URI=SecretStr("mongodb://127.0.0.1:27017"),
        FLIGHT_MONGO_DB="flight_data",
        SCAT_FLIGHTS_COLLECTION="scat_flights",
        SCAT_AIRSPACE_COLLECTION="airspace_configs",
        ANALYSIS_IDENTITY_MODE="disabled",
        ANALYSIS_SINGLE_USER_ID=None,
    )


def test_valid_runtime_configuration() -> None:
    validate_runtime_configuration(
        _valid_settings(),
        default_agent="scat-flight-agent",
        agent_keys=["scat-flight-agent"],
    )


def test_scat_configuration_does_not_require_an_llm() -> None:
    settings = _valid_settings()
    settings.DEFAULT_MODEL = None
    settings.AVAILABLE_MODELS = set()

    validate_scat_configuration(settings)


def test_single_user_identity_requires_a_stable_owner() -> None:
    settings = _valid_settings()
    settings.ANALYSIS_IDENTITY_MODE = "single_user"

    with pytest.raises(RuntimeError, match="ANALYSIS_SINGLE_USER_ID"):
        validate_scat_configuration(settings)

    settings.ANALYSIS_SINGLE_USER_ID = "local-researcher"
    validate_scat_configuration(settings)


def test_rejects_unregistered_default_agent() -> None:
    with pytest.raises(RuntimeError, match="is not registered"):
        validate_runtime_configuration(
            _valid_settings(),
            default_agent="missing-agent",
            agent_keys=["scat-flight-agent"],
        )


def test_rejects_default_model_without_configured_provider() -> None:
    settings = _valid_settings()
    settings.DEFAULT_MODEL = OpenAICompatibleName.OPENAI_COMPATIBLE

    with pytest.raises(RuntimeError) as exc_info:
        validate_runtime_configuration(
            settings,
            default_agent="scat-flight-agent",
            agent_keys=["scat-flight-agent"],
        )

    message = str(exc_info.value)
    assert "DEFAULT_MODEL" in message
    assert "COMPATIBLE_BASE_URL" in message
    assert "COMPATIBLE_MODEL" in message
    assert "COMPATIBLE_API_KEY" in message


def test_rejects_invalid_mongo_names_without_exposing_uri_secret() -> None:
    settings = _valid_settings()
    settings.FLIGHT_MONGO_URI = SecretStr("https://user:secret@example.test")
    settings.FLIGHT_MONGO_DB = ""

    with pytest.raises(RuntimeError) as exc_info:
        validate_runtime_configuration(
            settings,
            default_agent="scat-flight-agent",
            agent_keys=["scat-flight-agent"],
        )

    message = str(exc_info.value)
    assert "FLIGHT_MONGO_URI" in message
    assert "FLIGHT_MONGO_DB" in message
    assert "secret" not in message
