"""Production overlay keeps business state, RAG settings and credentials separated."""

from copy import deepcopy
from pathlib import Path, PurePosixPath

import pytest

import scripts.deploy_visualization as deployment


@pytest.fixture
def active(monkeypatch):
    monkeypatch.setattr(deployment, "SOURCE", Path(__file__).resolve().parents[2])
    root = PurePosixPath(deployment.ROOT.as_posix())
    shared = {
        "type": "bind",
        "source": str(root / "var/deploy/h7b/shared"),
        "target": str(root / "var/deploy/h7b/shared"),
        "read_only": False,
    }
    policy_mounts = [
        {
            "type": "bind",
            "source": str(root / name),
            "target": str(root / name),
            "read_only": readonly,
        }
        for name, readonly in [
            ("var/deploy/autonomy/config.json", True),
            ("var/deploy/autonomy/secrets/identity.key", True),
            ("var/autonomy/memory", False),
        ]
    ]
    services = {
        role: {
            "image": "prior-" + role,
            "volumes": [deepcopy(shared)],
            "environment": {"SCAT_RAG_BASELINE_ENABLED": "false", "BUSINESS_SETTING": "preserved"},
            "env_file": ["/private/existing.env"],
        }
        for role in deployment.UPDATED
    }
    services["platform"]["environment"]["SCAT_AUTONOMY_CONFIG"] = "var/deploy/autonomy/config.json"
    services["platform"]["volumes"] += policy_mounts
    services["hermes-main"]["volumes"].append(
        {
            "type": "bind",
            "source": str(root / "scripts/run_container_service.py"),
            "target": str(root / "scripts/run_container_service.py"),
            "read_only": True,
        }
    )
    return {"name": "scat-hermes-h7b", "services": services}


def candidate(active):
    return deployment.compose_candidate(
        active, {role: "new-" + role for role in deployment.UPDATED}, "sha256:" + "1" * 64
    )


def test_overlay_preserves_existing_business_settings_and_shared_paths(active):
    original = deepcopy(active)
    result = candidate(active)
    assert active == original
    assert result["name"] == original["name"]
    for role in deployment.UPDATED:
        service = result["services"][role]
        assert service["environment"]["SCAT_RAG_BASELINE_ENABLED"] == "false"
        assert service["environment"]["BUSINESS_SETTING"] == "preserved"
        assert service["env_file"] == original["services"][role]["env_file"]
        assert service["volumes"][0] == original["services"][role]["volumes"][0]


def test_model_credentials_are_only_supplied_to_separate_worker(active):
    services = candidate(active)["services"]
    for role in deployment.UPDATED:
        assert all("visualization-worker.env" not in name for name in services[role]["env_file"])
    worker = services["visualization-worker"]
    assert len(worker["env_file"]) == 1 and worker["env_file"][0].endswith(
        "visualization-worker.env"
    )
    assert all("docker.sock" not in mount["source"] for mount in worker["volumes"])
    assert worker["read_only"] and worker["cap_drop"] == ["ALL"]


def test_native_control_and_worker_share_authoritative_preference_state(active):
    services = candidate(active)["services"]
    for role in ("actions", "visualization-worker"):
        assert (
            services[role]["environment"]["SCAT_AUTONOMY_CONFIG"]
            == "var/deploy/autonomy/config.json"
        )
        mounts = {mount["target"]: mount for mount in services[role]["volumes"]}
        assert mounts[(deployment.ROOT / "var/deploy/autonomy/secrets/identity.key").as_posix()][
            "read_only"
        ]
        assert not mounts[(deployment.ROOT / "var/autonomy/memory").as_posix()]["read_only"]


def test_old_entrypoint_mount_cannot_hide_new_native_role_code(active):
    result = candidate(active)
    assert all(
        mount["target"] != (deployment.ROOT / "scripts/run_container_service.py").as_posix()
        for mount in result["services"]["hermes-main"]["volumes"]
    )


def test_build_pins_local_base_before_offline_build(tmp_path, monkeypatch):
    monkeypatch.setattr(deployment, "STATE", tmp_path)
    (tmp_path / "build").mkdir()
    images = {role: "sha256:" + str(i) * 64 for i, role in enumerate(deployment.UPDATED, 1)}
    deployment.save(tmp_path / "before.json", {"images": images})
    commands = []
    monkeypatch.setattr(deployment, "run", lambda args, **kwargs: commands.append(args) or "")
    deployment.build()
    for role in deployment.UPDATED:
        tag = deployment.IMAGE_PREFIX + "-base-" + role + ":20261128"
        assert ["docker", "tag", images[role], tag] in commands
        assert (tmp_path / "build" / (role + ".Dockerfile")).read_text().splitlines()[
            0
        ] == "FROM " + tag
    builds = [command for command in commands if command[:2] == ["docker", "build"]]
    assert len(builds) == 3 and all(
        "--pull=false" in command and "--network=none" in command for command in builds
    )
    native = next(
        command
        for command in commands
        if command[:2] == ["docker", "run"]
        and deployment.IMAGE_PREFIX + "-hermes-main:20261128" in command
    )
    assert "VisualizationBridge" in native[-1]
    assert "VisualizationWorker" not in native[-1] and "service.visualization_api" not in native[-1]


def test_host_launcher_refreshes_authorized_docker_group():
    root = Path(__file__).resolve().parents[2]
    unit = (root / "infra/systemd/user/scat-visualization-sandbox.service").read_text()
    assert (
        'ExecStart=/usr/bin/sg docker -c "%h/anaconda3/envs/scat-agent/bin/python -B scripts/run_visualization_sandbox.py"'
        in unit
    )
    assert "visualization-worker.env" not in unit


def test_worker_inherits_proven_native_model_dns_without_mutating_it(active):
    main = active["services"]["hermes-main"]
    main["dns"] = ["192.0.2.53", "192.0.2.54"]
    main["dns_opt"] = ["timeout:2", "attempts:2"]
    worker = candidate(active)["services"]["visualization-worker"]
    assert worker["dns"] == main["dns"] and worker["dns_opt"] == main["dns_opt"]
    worker["dns"].append("192.0.2.55")
    assert main["dns"] == ["192.0.2.53", "192.0.2.54"]


def test_worker_does_not_invent_public_dns_if_native_uses_host_resolver(active):
    worker = candidate(active)["services"]["visualization-worker"]
    assert "dns" not in worker and "dns_search" not in worker and "dns_opt" not in worker
