"""Prevent the observed accidental public-template key from entering an image."""

import json

import pytest

from scripts.prepare_container_context import reject_honcho_template_credentials


@pytest.mark.parametrize(
    "value",
    [
        {"apiKey": "synthetic-private-key"},
        {"hosts": {"hermes_scat-main": {"apiKey": "synthetic-private-key"}}},
    ],
)
def test_public_client_key_is_rejected_without_echoing_it(tmp_path, value):
    path = tmp_path / "honcho.json.example"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="^credential_in_public_honcho_template$"):
        reject_honcho_template_credentials(path)


@pytest.mark.parametrize("name", ["LLM_OPENAI_API_KEY", "AUTH_JWT_SECRET", "DB_CONNECTION_URI"])
def test_public_server_secret_is_rejected(tmp_path, name):
    path = tmp_path / "server.env.example"
    path.write_text(name + "=synthetic-private-value\n")
    with pytest.raises(ValueError, match="^credential_in_public_honcho_template$"):
        reject_honcho_template_credentials(path)


def test_blank_keys_and_environment_variable_references_remain_valid(tmp_path):
    client = tmp_path / "honcho.json.example"
    client.write_text(json.dumps({"apiKey": "", "hosts": {"hermes_scat-main": {"enabled": True}}}))
    reject_honcho_template_credentials(client)
    server = tmp_path / "server.env.example"
    server.write_text(
        "LLM_OPENAI_API_KEY=\nAUTH_JWT_SECRET=''\nEMBEDDING_MODEL_CONFIG__OVERRIDES__API_KEY_ENV=HONCHO_EMBEDDING_API_KEY\n"
    )
    reject_honcho_template_credentials(server)
