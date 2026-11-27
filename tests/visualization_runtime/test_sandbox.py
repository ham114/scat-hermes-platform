"""Docker restrictions and fail-closed checks without a local daemon."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.visualization_runtime.test_pipeline import png
from visualization_runtime.sandbox import SIZES, DockerSandbox, RenderError


def sandbox(tmp_path, *, mode="ok"):
    seccomp = tmp_path / "services/visualization-renderer/seccomp.json"
    seccomp.parent.mkdir(parents=True)
    shutil.copyfile(
        Path(__file__).parents[2] / "services/visualization-renderer/seccomp.json", seccomp
    )
    calls = []
    output = tmp_path / "case/output"

    def execute(args, **kwargs):
        calls.append(args)
        if args[1] == "start":
            if mode == "timeout":
                raise subprocess.TimeoutExpired(args, 1)
            images = []
            for viewport, (width, height) in SIZES.items():
                (output / (viewport + ".png")).write_bytes(png(viewport))
                images.append(
                    {
                        "viewport": viewport,
                        "width": width,
                        "height": height,
                        "file": viewport + ".png",
                        "ink_ratio": 0 if mode == "blank" else 0.5,
                        "contrast": 255,
                    }
                )
            (output / "manifest.json").write_text(
                json.dumps({"state": "rendered", "images": images})
            )
        if args[1:3] == ["container", "inspect"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "Config": {
                                "Labels": {
                                    "com.scat.visualization": "other" if mode == "foreign" else "1"
                                }
                            }
                        }
                    ]
                ),
            )
        return SimpleNamespace(returncode=1 if mode == "unavailable" else 0, stdout="")

    return DockerSandbox(tmp_path, "sha256:" + "a" * 64, execute=execute), calls


def test_generated_code_gets_fixed_mounts_and_restricted_container(tmp_path):
    runner, calls = sandbox(tmp_path)
    result = runner.render(
        "vis-" + "a" * 32 + "-g1",
        tmp_path / "case",
        {
            "source": "export async function render() {}",
            "snapshot": {},
            "viewports": ["desktop", "mobile"],
        },
    )
    assert result["state"] == "rendered" and len(result["images"]) == 2
    create = next(c for c in calls if c[1] == "create")
    for flag, value in (
        ("--network", "none"),
        ("--user", "1000:1000"),
        ("--cap-drop", "ALL"),
        ("--memory", "1g"),
    ):
        assert create[create.index(flag) + 1] == value
    assert "--read-only" in create and "no-new-privileges:true" in create
    assert not any("docker.sock" in v or "unconfined" in v or "API_KEY" in v for v in create)
    assert any(c[1:3] == ["rm", "--force"] for c in calls)


@pytest.mark.parametrize("mode,error", [("timeout", "render_timeout"), ("blank", "blank_render")])
def test_timeout_or_blank_output_cannot_pass_and_container_is_removed(tmp_path, mode, error):
    runner, calls = sandbox(tmp_path, mode=mode)
    with pytest.raises(RenderError, match=error):
        runner.render(
            "vis-" + "a" * 32 + "-g1",
            tmp_path / "case",
            {"source": "code", "snapshot": {}, "viewports": ["desktop", "mobile"]},
        )
    assert any(c[1:3] == ["rm", "--force"] for c in calls)


def test_cleanup_never_removes_foreign_container(tmp_path):
    runner, calls = sandbox(tmp_path, mode="foreign")
    with pytest.raises(RenderError, match="not_owned"):
        runner.remove("vis-" + "a" * 32 + "-g1")
    assert not any(c[1] == "rm" for c in calls)


def test_daemon_failure_is_not_evidence_of_cleanup(tmp_path):
    runner, _ = sandbox(tmp_path, mode="unavailable")
    assert runner.stopped("vis-" + "a" * 32 + "-g1") is False
    with pytest.raises(ValueError):
        runner.stopped("production-container")
