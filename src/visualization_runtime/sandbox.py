"""Trusted, fixed Docker launcher. Generated source is never evaluated by Python."""

import hashlib
import json
import re
import struct
import subprocess
from pathlib import Path

from visualization_runtime.json_codec import canonical
from visualization_runtime.paths import confined_path

SIZES = {"desktop": (1200, 720), "mobile": (390, 640)}
SECCOMP_SHA = "edcc7ec801fcc0d02257df33ff617212202f8dd82b66227bfb8513d5620761c6"


class RenderError(RuntimeError):
    pass


def file_bytes(path, *, maximum=15 * 1024 * 1024):
    if path.is_symlink() or not path.is_file():
        raise RenderError("render_file_unavailable")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise RenderError("render_file_too_large")
    return raw


def validate_png(raw, viewport):
    if len(raw) < 33 or raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        raise RenderError("invalid_render_png")
    if struct.unpack("!II", raw[16:24]) != SIZES[viewport]:
        raise RenderError("render_dimensions_changed")


class DockerSandbox:
    def __init__(self, root: Path, image: str, *, timeout=30, execute=subprocess.run):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("pinned_sandbox_image_required")
        if type(timeout) is not int or not 1 <= timeout <= 120:
            raise ValueError("invalid_render_timeout")
        self.root, self.image, self.timeout, self.execute = root.resolve(), image, timeout, execute
        self.seccomp = confined_path(root, root / "services/visualization-renderer/seccomp.json")
        if hashlib.sha256(self.seccomp.read_bytes()).hexdigest() != SECCOMP_SHA:
            raise RenderError("sandbox_seccomp_changed")

    @staticmethod
    def _name(name):
        if not re.fullmatch(r"vis-[0-9a-f]{32}-g[0-9]{1,9}", name):
            raise ValueError("invalid_sandbox_resource_id")

    def _run(self, args, *, timeout=10):
        return self.execute(
            ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
        )

    def stopped(self, name):
        self._name(name)
        try:
            result = self._run(
                ["ps", "-a", "--filter", "name=^/" + name + "$", "--format", "{{.State}}"]
            )
            return result.returncode == 0 and result.stdout.strip() in {
                "",
                "exited",
                "dead",
                "created",
            }
        except (OSError, subprocess.TimeoutExpired):
            return False

    def remove(self, name):
        self._name(name)
        try:
            result = self._run(["container", "inspect", name])
            if result.returncode:
                if not self.stopped(name):
                    raise RenderError("sandbox_cleanup_unverified")
                return
            record = json.loads(result.stdout)[0]
            if record.get("Config", {}).get("Labels", {}).get("com.scat.visualization") != "1":
                raise RenderError("sandbox_resource_not_owned")
            result = self._run(["rm", "--force", name])
            if result.returncode or not self.stopped(name):
                raise RenderError("sandbox_cleanup_unverified")
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, IndexError):
            raise RenderError("sandbox_cleanup_unverified") from None

    def render(self, name, directory, request):
        self._name(name)
        directory = confined_path(self.root, directory)
        inputs, outputs = directory / "input", directory / "output"
        inputs.mkdir(parents=True, exist_ok=False)
        outputs.mkdir()
        (inputs / "request.json").write_text(canonical(request), encoding="utf-8")
        args = [
            "create",
            "--name",
            name,
            "--label",
            "com.scat.visualization=1",
            "--network",
            "none",
            "--read-only",
            "--user",
            "1000:1000",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--security-opt",
            "seccomp=" + str(self.seccomp),
            "--pids-limit",
            "256",
            "--cpus",
            "1",
            "--memory",
            "1g",
            "--memory-swap",
            "1g",
            "--shm-size",
            "128m",
            "--tmpfs",
            "/tmp:rw,nosuid,size=128m",
            "--log-driver",
            "none",
            "--mount",
            f"type=bind,source={inputs},target=/input,readonly",
            "--mount",
            f"type=bind,source={outputs},target=/output",
            self.image,
        ]
        created = False
        try:
            created = True  # A timed-out create may already have reached Docker.
            result = self._run(args, timeout=15)
            if result.returncode:
                raise RenderError("sandbox_create_failed")
            result = self._run(["start", "--attach", name], timeout=self.timeout)
            if result.returncode:
                raise RenderError("render_execution_failed")
            manifest = json.loads(file_bytes(outputs / "manifest.json", maximum=16384))
            if manifest.get("state") != "rendered":
                raise RenderError("render_execution_failed")
            images = manifest.get("images", [])
            if len(images) != len(request["viewports"]) or {
                i.get("viewport") for i in images
            } != set(request["viewports"]):
                raise RenderError("render_viewports_incomplete")
            for item in images:
                viewport = item["viewport"]
                if viewport not in SIZES or item.get("file") != viewport + ".png":
                    raise RenderError("invalid_render_file")
                raw = file_bytes(outputs / item["file"])
                validate_png(raw, viewport)
                if (
                    not isinstance(item.get("ink_ratio"), (int, float))
                    or not 0.001 < item["ink_ratio"] <= 1
                    or not 16 <= item.get("contrast", -1) <= 255
                ):
                    raise RenderError("blank_render")
                item["sha256"] = hashlib.sha256(raw).hexdigest()
            return {
                "state": "rendered",
                "images": images,
                "runtime_image": self.image,
                "seccomp_sha256": SECCOMP_SHA,
            }
        except subprocess.TimeoutExpired:
            raise RenderError("render_timeout") from None
        except (OSError, ValueError, KeyError, TypeError):
            raise RenderError("render_output_invalid") from None
        finally:
            if created:
                self.remove(name)
