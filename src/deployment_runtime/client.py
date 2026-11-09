"""Bounded internal HTTP transport; credentials and identities stay outside model arguments."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit


def private_origin(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("private_loopback_origin_required")
    if not parsed.port or not 18120 <= parsed.port <= 18139:
        raise ValueError("private_service_port_required")
    return value.rstrip("/")


def credential(root, relative):
    root = Path(root).resolve()
    original = root / relative
    path = original.resolve()
    if not path.is_relative_to(root / "var") or original.is_symlink():
        raise ValueError("project_private_credential_required")
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise ValueError("private_credential_requires_0600")
    value = path.read_text().strip()
    if len(value) < 32:
        raise ValueError("private_credential_missing")
    return value


def action_request(root, environment, namespace, payload, *, schemas=False, timeout=35):
    import httpx

    origin = private_origin(environment["SCAT_ACTION_BASE_URL"])
    token = credential(root, environment["SCAT_ACTION_KEY_FILE"])
    headers = {"Authorization": "Bearer " + token}
    path = "/v1/schemas/" + namespace if schemas else "/v1/actions/" + namespace
    try:
        with httpx.Client(
            base_url=origin, headers=headers, timeout=timeout, trust_env=False
        ) as client:
            response = client.get(path) if schemas else client.post(path, json=payload)
            if response.status_code in {401, 403}:
                return {"status": "forbidden"}
            if response.status_code == 429:
                return {"status": "busy", "automatic_replay": False}
            response.raise_for_status()
            if len(response.content) > 524288:
                raise ValueError("private_response_too_large")
            return response.json()
    except httpx.TransportError:
        return {"status": "unavailable" if schemas else "unknown", "automatic_replay": False}
    except httpx.HTTPStatusError:
        return {"status": "unavailable" if schemas else "unknown", "automatic_replay": False}
