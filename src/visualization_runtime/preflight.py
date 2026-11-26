"""Two small live probes using synthetic input; generated code is never executed."""

import asyncio
import base64
import json
import re
import struct
import time
import zlib

from visualization_runtime.providers import ProviderError, complete
from visualization_runtime.settings import ConfigurationError, VisualizationSettings


def fixture_png() -> bytes:
    """Three colored bars with bitmap category labels; no imaging dependencies."""
    width, height = 300, 220
    rows = [bytearray(b"\xff\xff\xff" * width) for _ in range(height)]

    def rect(left, top, right, bottom, color):
        for y in range(top, bottom):
            rows[y][left * 3 : right * 3] = bytes(color) * (right - left)

    rect(20, 185, 280, 188, (0, 0, 0))
    for x, top, color in (
        (45, 125, (234, 88, 12)),
        (125, 35, (37, 99, 235)),
        (205, 95, (22, 163, 74)),
    ):
        rect(x, top, x + 45, 185, color)
    glyphs = (
        ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
        ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
        ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    )
    for x, glyph in zip((60, 140, 220), glyphs):
        for y, row in enumerate(glyph):
            for col, bit in enumerate(row):
                if bit == "1":
                    rect(x + col * 3, 193 + y * 3, x + col * 3 + 3, 196 + y * 3, (0, 0, 0))

    def chunk(kind, data):
        return (
            struct.pack("!I", len(data))
            + kind
            + data
            + struct.pack("!I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\0" + bytes(row) for row in rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def code_probe_valid(content):
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return bool(re.search(r"export\s+(?:async\s+)?function\s+render\s*\(", text)) and (
        "echarts" in text and "setOption" in text
    )


def vision_probe_valid(content):
    try:
        value = json.loads(content)
    except (ValueError, TypeError):
        return False
    return (
        isinstance(value, dict)
        and set(value) == {"highest_label", "highest_color", "bar_count"}
        and value["highest_label"] == "B"
        and value["highest_color"] == "blue"
        and type(value["bar_count"]) is int
        and value["bar_count"] == 3
    )


async def run_preflight(
    settings: VisualizationSettings, *, live=False, transport=None, roles=("code", "review")
):
    if not roles or len(set(roles)) != len(roles) or set(roles) - {"code", "review"}:
        raise ValueError("invalid_preflight_roles")

    async def probe(role):
        result = {"role": role, "state": "configuration_valid", "request_count": 0}
        try:
            config = settings.provider(role)
        except ConfigurationError as error:
            return result | {"state": "configuration_missing", "error": str(error)}
        result["model"] = config.model
        if not live:
            return result
        if role == "code":
            messages = [
                {
                    "role": "user",
                    "content": (
                        "Return only JavaScript exporting async function render({echarts,container,data}). "
                        'Initialize an ECharts chart and setOption for data=[{label:"A",value:2},'
                        '{label:"B",value:5},{label:"C",value:3}]. No imports or network calls. '
                        "This is a synthetic API probe, not a user chart."
                    ),
                }
            ]
        else:
            uri = "data:image/png;base64," + base64.b64encode(fixture_png()).decode()
            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Inspect the attached chart image. Return only a JSON object with exactly "
                                "highest_label (category letter of the tallest bar), highest_color "
                                "(basic English color name of that bar), and bar_count (integer)."
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": uri}},
                    ],
                }
            ]
        started = time.monotonic()
        result["request_count"] = 1
        try:
            response = await complete(
                config, messages, max_tokens=1200, json_object=role == "review", transport=transport
            )
            valid = (code_probe_valid if role == "code" else vision_probe_valid)(response.content)
            result.update(
                state="passed" if valid else "probe_validation_failed",
                usage=response.usage,
                response_model_matches=response.model_matches,
                provider_request_id=response.request_id,
            )
        except ProviderError as error:
            result.update(
                state="failed",
                error=error.code,
                http_status=error.status,
                outcome_unknown=error.unknown,
                usage=error.usage,
            )
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return result

    results = await asyncio.gather(*(probe(role) for role in roles))
    return {
        "schema_version": 1,
        "live": live,
        "feature_enabled": settings.enabled,
        "passed": all(r["state"] == ("passed" if live else "configuration_valid") for r in results),
        "probes": results,
    }
