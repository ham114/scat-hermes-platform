"""Snapshot only reviewed source into the Docker context and record every file hash."""

import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "var/deploy/h7b/context"


def reject_honcho_template_credentials(path):
    """Fail before copying public Honcho templates that contain filled secrets."""
    if path.name == "honcho.json.example":

        def check(value):
            if isinstance(value, dict):
                for name, item in value.items():
                    if name in {
                        "apiKey",
                        "api_key",
                        "access_token",
                        "refresh_token",
                    } and item not in (None, ""):
                        raise ValueError("credential_in_public_honcho_template")
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)

        check(json.loads(path.read_text(encoding="utf-8")))
    elif path.name == "server.env.example":
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            name, separator, value = line.partition("=")
            if (
                separator
                and (
                    name.endswith(("_API_KEY", "_SECRET", "_PASSWORD"))
                    or name == "DB_CONNECTION_URI"
                )
                and value.strip().strip("\"'")
            ):
                raise ValueError("credential_in_public_honcho_template")


def main():
    destination = OUT / "app"
    manifest = {}
    banned = {
        ".git",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "venv",
    }
    paths = []
    for name in ["src", "scripts", "config", "services", "skills", "vendor/hermes-agent"]:
        paths.extend(p for p in (ROOT / name).rglob("*") if p.is_file())
    paths.extend(ROOT / name for name in ["pyproject.toml", "LICENSE", "NOTICE.md"])
    for path in sorted(paths):
        relative = path.relative_to(ROOT)
        if relative.parts[:2] == ("config", "honcho"):
            reject_honcho_template_credentials(path)
        if (
            any(part in banned for part in relative.parts)
            or path.name == ".env"
            or path.suffix
            in {".pyc", ".key", ".db", ".sqlite", ".log", ".safetensors", ".pt", ".pth", ".gguf"}
        ):
            continue
        if path.is_symlink():
            if not path.resolve().is_relative_to(ROOT):
                raise ValueError("external_source_link")
            continue
        output = destination / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, output)
        manifest[relative.as_posix()] = hashlib.sha256(output.read_bytes()).hexdigest()
    # An older snapshot must not leave files outside the reviewed manifest.
    for path in destination.rglob("*"):
        if path.is_file() and path.relative_to(destination).as_posix() not in manifest:
            if path.is_symlink() or not path.resolve().is_relative_to(destination.resolve()):
                raise ValueError("unsafe_stale_context_path")
            path.unlink()
    (OUT / "source.lock.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (destination / "var").mkdir(exist_ok=True)
    (destination / ".tmp").mkdir(exist_ok=True)
    shutil.copyfile(ROOT / "infra/compose/Dockerfile.cpu", OUT / "Dockerfile.cpu")
    (OUT / "Dockerfile.author").write_text(
        "ARG BUSINESS_IMAGE=scat-hermes-h7b-business:20261027\nFROM ${BUSINESS_IMAGE}\nCOPY --from=scat-hermes-h7b-native:20261027 /opt/conda/envs/scat-hermes/ /opt/conda/envs/scat-hermes/\n"
    )
    print(
        json.dumps(
            {
                "source_files": len(manifest),
                "source_bytes": sum((destination / name).stat().st_size for name in manifest),
            }
        )
    )


if __name__ == "__main__":
    main()
