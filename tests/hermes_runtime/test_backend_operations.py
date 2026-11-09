"""Operations boundaries that protect existing processes, credentials and durable state."""

import io
import json
import os
import sys
from types import SimpleNamespace

import pytest

from backend_ops import environment as freeze
from backend_ops import runtime as ops


def configured(tmp_path, **changes):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(ops.DEFAULTS | changes))
    return ops.Stack(tmp_path, "config.json")


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@repo.anaconda.com/a.conda",
        "https://repo.anaconda.com/t/secret/a.conda",
        "https://repo.anaconda.com/a.conda?token=secret",
        "https://private.invalid/a.conda",
        "file:///home/user/private",
        "http://repo.anaconda.com/a.conda",
    ],
)
def test_environment_sources_do_not_export_credentials(url):
    with pytest.raises(ValueError):
        freeze.public_url(url)


@pytest.mark.parametrize("package", ["x\n--index-url=secret", "a@https://private", "a b"])
def test_package_names_cannot_inject_requirement_options(package):
    with pytest.raises(ValueError):
        freeze.pin(package, "1.0")


def test_noarch_conda_distribution_is_not_reinstalled_by_pip(tmp_path, monkeypatch):
    prefix = tmp_path / "conda"
    meta = prefix / "conda-meta"
    meta.mkdir(parents=True)
    member = "site-packages/packaging-26.2.dist-info/METADATA"
    (meta / "packaging.json").write_text(
        json.dumps(
            {
                "url": "https://repo.anaconda.com/pkgs/main/linux-64/packaging.conda",
                "sha256": "a" * 64,
                "files": [member],
            }
        )
    )
    dist = SimpleNamespace(
        metadata={"Name": "packaging"},
        version="26.2",
        _path=prefix
        / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages/packaging-26.2.dist-info",
        read_text=lambda _: '{"url":"file:///build/private/work"}',
    )
    monkeypatch.setattr(sys, "prefix", str(prefix))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(freeze.importlib.metadata, "distributions", lambda: [dist])
    result = freeze.inventory(tmp_path)
    assert result["pip"] == [] and result["local"] == []
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize(
    "changes",
    [
        {"platform_port": 8080},
        {"hermes_port": 18120},
        {"instance": "../other"},
        {"log_bytes": True},
        {"max_hours": 0},
        {"max_used_percent": 100},
        {"extra_command": "sh"},
    ],
)
def test_config_refuses_production_ports_paths_and_commands(tmp_path, changes):
    with pytest.raises(ValueError):
        configured(tmp_path, **changes)


def test_configuration_rejects_symlink_components(tmp_path):
    outside = tmp_path / "other"
    outside.mkdir()
    link = tmp_path / "alias"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError):
        ops.inside(tmp_path, "alias/config.json")


def test_rotating_stream_is_bounded_private_and_preserves_latest_bytes(tmp_path):
    sink = ops.RotatingSink(tmp_path, tmp_path / "output.log", 4096, 2)
    for i in range(10):
        sink.write(bytes([65 + i]) * 4096)
    files = list(tmp_path.glob("output.log*"))
    assert len(files) == 3 and sum(p.stat().st_size for p in files) == 3 * 4096
    assert (tmp_path / "output.log").read_bytes() == b"J" * 4096
    if sys.platform == "linux":
        assert all(p.stat().st_mode & 0o077 == 0 for p in files)


def test_log_rotation_never_replaces_symlink_target(tmp_path):
    target = tmp_path / "protected"
    target.write_bytes(b"keep")
    try:
        (tmp_path / "output.log.1").symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    sink = ops.RotatingSink(tmp_path, tmp_path / "output.log", 4096, 1)
    sink.write(b"a" * 4096)
    with pytest.raises(ValueError):
        sink.write(b"b")
    assert target.read_bytes() == b"keep"


def test_disk_threshold_counts_both_free_bytes_and_percentage(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ops.shutil, "disk_usage", lambda _: SimpleNamespace(total=1000, used=950, free=50)
    )
    config = ops.DEFAULTS | {"min_free_bytes": 40}
    assert ops.disk_status(tmp_path, config)["warning"]
    assert ops.disk_status(tmp_path, config)["ok"]
    assert not ops.disk_status(tmp_path, config | {"min_free_bytes": 60})["ok"]
    assert not ops.disk_status(tmp_path, config | {"max_used_percent": 95})["ok"]


def test_stale_pid_identity_does_not_send_signal(tmp_path, monkeypatch):
    stack = configured(tmp_path)
    stack.home.mkdir(parents=True)
    freeze.dump(
        stack.state_path,
        json.dumps(
            {
                "status": "ready",
                "controller": {"pid": os.getpid(), "birth": "wrong", "boot": "wrong"},
                "token": "a" * 32,
                "children": [],
            }
        ),
    )
    monkeypatch.setattr(ops, "process_identity", lambda _: None)
    monkeypatch.setattr(ops.os, "kill", lambda *_: pytest.fail("must not kill reused PID"))
    monkeypatch.setattr(ops.os, "killpg", lambda *_: pytest.fail("must not kill unrelated group"))
    monkeypatch.setattr(ops.time, "sleep", lambda _: None)
    assert stack.status()["status"] == "stale"
    assert stack.stop()["status"] == "stopped"


def test_private_service_key_is_stable_and_never_in_status(tmp_path):
    stack = configured(tmp_path)
    stack.home.mkdir(parents=True)
    key = ops.private_key(tmp_path, stack.home / "platform-auth.key")
    assert key == ops.private_key(tmp_path, stack.home / "platform-auth.key")
    assert len(key) >= 32 and key not in json.dumps(stack.status())


@pytest.mark.skipif(sys.platform != "linux", reason="Linux filesystem permissions")
def test_publicly_readable_key_is_refused(tmp_path):
    path = tmp_path / "key"
    path.write_text("a" * 48)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private_service_key_required"):
        ops.private_key(tmp_path, path)


def test_atomic_manifest_update_does_not_follow_final_symlink(tmp_path):
    target = tmp_path / "protected"
    target.write_text("keep")
    try:
        (tmp_path / "state.json").symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError):
        freeze.dump(tmp_path / "state.json", "replacement")
    assert target.read_text() == "keep"


def test_environment_verification_detects_modified_reconstruction_input(tmp_path):
    snapshot = {
        "conda": [{"url": "https://repo.anaconda.com/a.conda", "checksum": "a" * 64}],
        "pip": ["a==1.0"],
        "local": [],
    }
    freeze.write_inputs(tmp_path, snapshot)
    assert freeze.verify_inputs(tmp_path, snapshot)
    (tmp_path / "pip-requirements.txt").write_text("a==2.0\n")
    assert not freeze.verify_inputs(tmp_path, snapshot)


def test_log_io_failure_is_visible_to_supervisor(tmp_path, monkeypatch):
    sink = ops.RotatingSink(tmp_path, tmp_path / "output.log", 4096, 1)

    def fail(_):
        raise OSError("synthetic_disk_full")

    monkeypatch.setattr(sink, "write", fail)
    sink.drain(io.BytesIO(b"synthetic"))
    assert sink.failed.is_set()
