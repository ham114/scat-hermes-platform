"""Register reviewed GBAR source and file digests in an existing dotenv file."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from core.file_integrity import file_sha256
from core.settings import settings


class RegistrationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    env_file: Path
    source_uri: str = Field(pattern=r"^[a-z][a-z0-9+.-]*://\S+$", max_length=500)
    apply: bool


def _required_file(value: str | None, name: str) -> Path:
    if not value:
        raise ValueError(f"{name} is not configured")
    return Path(value).expanduser().resolve(strict=True)


def _updated_dotenv(text: str, values: dict[str, str]) -> str:
    remaining = dict(values)
    output: list[str] = []
    assignment = re.compile(r"^([A-Z][A-Z0-9_]*)=")
    for line in text.splitlines():
        match = assignment.match(line)
        key = match.group(1) if match else None
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    if remaining:
        if output and output[-1]:
            output.append("")
        output.append("# Registered model provenance (M9-18).")
        output.extend(f"{key}={value}" for key, value in remaining.items())
    return "\n".join(output) + "\n"


def run(config: RegistrationInput) -> dict[str, object]:
    env_file = config.env_file.expanduser().resolve(strict=True)
    files = {
        "SCAT_GBAR_V23_WORKER_SCRIPT_SHA256": _required_file(
            settings.SCAT_GBAR_V23_WORKER_SCRIPT, "SCAT_GBAR_V23_WORKER_SCRIPT"
        ),
        "SCAT_GBAR_V23_CONFIG_SHA256": _required_file(
            settings.SCAT_GBAR_V23_CONFIG, "SCAT_GBAR_V23_CONFIG"
        ),
        "SCAT_GBAR_V23_CHECKPOINT_SHA256": _required_file(
            settings.SCAT_GBAR_V23_CHECKPOINT, "SCAT_GBAR_V23_CHECKPOINT"
        ),
    }
    values = {
        "SCAT_GBAR_V23_SOURCE_URI": config.source_uri,
        **{key: file_sha256(path) for key, path in files.items()},
    }
    if config.apply:
        current = env_file.read_text(encoding="utf-8")
        updated = _updated_dotenv(current, values)
        mode = env_file.stat().st_mode
        fd, temporary_name = tempfile.mkstemp(prefix=f".{env_file.name}.", dir=env_file.parent)
        try:
            os.close(fd)
            temporary = Path(temporary_name)
            temporary.write_text(updated, encoding="utf-8")
            os.chmod(temporary, mode)
            os.replace(temporary, env_file)
        finally:
            Path(temporary_name).unlink(missing_ok=True)
    return {
        "status": "registered" if config.apply else "preview",
        "source_uri": config.source_uri,
        "digests": {key: value for key, value in values.items() if key.endswith("SHA256")},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--source-uri", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        config = RegistrationInput.model_validate(vars(args))
        if not config.apply:
            raise ValueError("--apply is required")
        report = run(config)
    except Exception as error:  # noqa: BLE001 - stable operational CLI boundary
        print(json.dumps({"status": "failed", "error": str(error)[:500]}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
