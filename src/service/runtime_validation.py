"""Startup validation for configuration that spans settings and registries."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from schema.models import DeepseekModelName, OpenAICompatibleName


def _scat_configuration_errors(settings: Any) -> list[str]:
    errors: list[str] = []
    mongo_uri = settings.FLIGHT_MONGO_URI.get_secret_value()
    if not mongo_uri.startswith(("mongodb://", "mongodb+srv://")):
        errors.append("FLIGHT_MONGO_URI must use mongodb:// or mongodb+srv://")

    named_values = {
        "FLIGHT_MONGO_DB": settings.FLIGHT_MONGO_DB,
        "SCAT_FLIGHTS_COLLECTION": settings.SCAT_FLIGHTS_COLLECTION,
        "SCAT_AIRSPACE_COLLECTION": settings.SCAT_AIRSPACE_COLLECTION,
    }
    for name, value in named_values.items():
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{name} must be a non-empty string")
    return errors


def _analysis_identity_configuration_errors(settings: Any) -> list[str]:
    mode = getattr(settings, "ANALYSIS_IDENTITY_MODE", "disabled")
    mode_value = getattr(mode, "value", mode)
    if mode_value != "single_user":
        return []

    owner_id = getattr(settings, "ANALYSIS_SINGLE_USER_ID", None)
    if (
        not isinstance(owner_id, str)
        or owner_id != owner_id.strip()
        or not (1 <= len(owner_id) <= 200)
        or any(ord(character) < 32 or ord(character) == 127 for character in owner_id)
    ):
        return [
            "ANALYSIS_SINGLE_USER_ID must be a 1-200 character printable value "
            "when ANALYSIS_IDENTITY_MODE=single_user"
        ]
    return []


def _agent_configuration_errors(
    settings: Any,
    *,
    default_agent: str,
    agent_keys: Iterable[str],
) -> list[str]:
    errors: list[str] = []
    available_agents = set(agent_keys)
    available_models = set(settings.AVAILABLE_MODELS)

    if default_agent not in available_agents:
        errors.append(f"DEFAULT_AGENT '{default_agent}' is not registered")

    if settings.DEFAULT_MODEL not in available_models:
        errors.append(
            f"DEFAULT_MODEL '{settings.DEFAULT_MODEL}' is not available "
            "with the configured providers"
        )

    if settings.DEFAULT_MODEL == OpenAICompatibleName.OPENAI_COMPATIBLE:
        if not settings.COMPATIBLE_BASE_URL:
            errors.append("COMPATIBLE_BASE_URL is required for openai-compatible")
        if not settings.COMPATIBLE_MODEL:
            errors.append("COMPATIBLE_MODEL is required for openai-compatible")
        if not settings.COMPATIBLE_API_KEY:
            errors.append("COMPATIBLE_API_KEY is required for openai-compatible")

    if settings.DEFAULT_MODEL == DeepseekModelName.DEEPSEEK_CHAT and not settings.DEEPSEEK_API_KEY:
        errors.append("DEEPSEEK_API_KEY is required for deepseek-chat")
    return errors


def validate_scat_configuration(settings: Any) -> None:
    """Validate only dependencies required by deterministic SCAT endpoints."""
    errors = _scat_configuration_errors(settings)
    errors.extend(_analysis_identity_configuration_errors(settings))
    if errors:
        raise RuntimeError("Invalid SCAT configuration: " + "; ".join(errors))


def validate_runtime_configuration(
    settings: Any,
    *,
    default_agent: str,
    agent_keys: Iterable[str],
) -> None:
    """Raise one safe error containing all startup configuration problems."""
    errors = _scat_configuration_errors(settings)
    errors.extend(_analysis_identity_configuration_errors(settings))
    errors.extend(
        _agent_configuration_errors(
            settings,
            default_agent=default_agent,
            agent_keys=agent_keys,
        )
    )

    if errors:
        raise RuntimeError("Invalid runtime configuration: " + "; ".join(errors))
