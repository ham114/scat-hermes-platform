"""Bounded synthetic provider checks. Never print credentials or provider errors."""

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/validation/20261027-honcho"


def main():
    if sys.executable != "/home/h3c/anaconda3/envs/scat-agent/bin/python":
        raise ValueError("existing_APP_Conda_required")
    import httpx
    from dotenv import dotenv_values

    os.umask(0o077)
    OUT.mkdir(parents=True, exist_ok=True)
    private = ROOT / "var/deploy/honcho/secrets/honcho.env"
    incoming = ROOT / "var/deploy/honcho/secrets/incoming-provider.env"
    if private.is_symlink() or private.stat().st_mode & 0o077:
        raise ValueError("unsafe_private_credential")
    values = dotenv_values(private)
    if incoming.exists():
        if incoming.is_symlink() or incoming.stat().st_mode & 0o077:
            raise ValueError("unsafe_private_import")
        imported = dotenv_values(incoming).get("LLM_OPENAI_API_KEY", "")
        if not imported or (
            values.get("LLM_OPENAI_API_KEY") and values["LLM_OPENAI_API_KEY"] != imported
        ):
            raise ValueError("private_provider_import_conflict")
        body = private.read_text()
        lines = body.splitlines()
        lines = [
            ("LLM_OPENAI_API_KEY=" + json.dumps(imported))
            if row.startswith("LLM_OPENAI_API_KEY=")
            else row
            for row in lines
        ]
        private.write_text("\n".join(lines) + "\n")
        private.chmod(0o600)
        incoming.unlink()
        values = dotenv_values(private)
    key = values.get("LLM_OPENAI_API_KEY")
    if not key:
        raise ValueError("provider_key_not_filled")
    marker = OUT / "provider-started.json"
    with marker.open("x") as stream:
        json.dump(
            {
                "max_requests": 4,
                "conservative_cap": 30000,
                "cases": ["literal-response", "required-tool"],
                "automatic_retry": False,
            },
            stream,
        )
    cases = [
        {
            "id": "literal-response",
            "body": {
                "messages": [{"role": "user", "content": "Reply with exactly HONCHO_TEST_OK."}],
                "max_tokens": 32,
            },
        },
        {
            "id": "required-tool",
            "body": {
                "messages": [
                    {
                        "role": "user",
                        "content": "Synthetic memory test: record language zh-CN and distance_unit km using the tool.",
                    }
                ],
                "max_tokens": 128,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "record_preference",
                            "description": "Record synthetic preferences",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "language": {"type": "string"},
                                    "distance_unit": {"type": "string"},
                                },
                                "required": ["language", "distance_unit"],
                                "additionalProperties": False,
                            },
                        },
                    }
                ],
                "tool_choice": "required",
            },
        },
    ]
    results = []
    with httpx.Client(trust_env=False, timeout=90) as http:
        for case in cases:
            body = {
                "model": "deepseek-flash",
                "temperature": 0,
                "stream": False,
                "thinking": {"type": "disabled"},
                **case["body"],
            }
            row = {
                "case": case["id"],
                "status": "failed",
                "conservative_reserved": len(json.dumps(body, ensure_ascii=False).encode())
                + body["max_tokens"],
            }
            started = time.monotonic()
            try:
                response = http.post(
                    "https://api.deepseek.com/chat/completions",
                    headers={"Authorization": "Bearer " + key},
                    json=body,
                )
                row["http_status"] = response.status_code
                if response.status_code == 200:
                    data = response.json()
                    row["usage"] = data.get("usage")
                    message = data["choices"][0]["message"]
                    if case["id"] == "literal-response":
                        passed = (message.get("content") or "").strip() == "HONCHO_TEST_OK"
                    else:
                        tools = message.get("tool_calls") or []
                        passed = (
                            len(tools) == 1
                            and tools[0]["function"]["name"] == "record_preference"
                            and json.loads(tools[0]["function"]["arguments"])
                            == {"language": "zh-CN", "distance_unit": "km"}
                        )
                    row["status"] = "passed" if passed else "failed"
            except Exception as exc:  # noqa: BLE001 -- never export raw provider exceptions
                row["error_type"] = type(exc).__name__
            row["seconds"] = round(time.monotonic() - started, 3)
            results.append(row)
            report = {
                "status": "passed"
                if len(results) == 2 and all(r["status"] == "passed" for r in results)
                else "failed",
                "attempts": results,
                "model_requests": len(results),
                "conservative_reserved": sum(r["conservative_reserved"] for r in results),
                "credential_values_exported": False,
            }
            (OUT / "provider-result.json").write_text(json.dumps(report, indent=2) + "\n")
            if row["status"] != "passed":
                break
    print(json.dumps(report))
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
