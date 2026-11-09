"""Production discovery and immutable registration/shared-receipt deployment contract."""

import importlib.util
from pathlib import Path

from workflow_runtime.catalog import describe

ROOT = Path(__file__).resolve().parents[2]


def test_catalog_exposes_actual_gbar_preprocessing_and_budget_limits(monkeypatch):
    from tests.workflows.test_gpu_tasks import config_body
    from workflow_runtime.gpu import GPURegistration

    config = GPURegistration.model_validate({**config_body(), "max_windows_per_job": 7})
    monkeypatch.setattr("workflow_runtime.gpu.registration", lambda root: config)
    catalog = describe()
    model = next(m for m in catalog["models"] if m["model_id"] == "scat-gbar-v23")
    assert model["version"] == "23" and model["parameters"] == {}
    assert model["constraints"] == {
        "min_observed_s": 350,
        "max_observation_points": 500,
        "model_resampled_points": 36,
        "model_interval_s": 10,
        "max_horizon_s": 360,
        "forecast_horizon_multiple_s": 10,
        "max_cutoffs_per_flight": 4,
        "max_windows_per_job": 7,
        "extra_context_allowed": False,
    }
    assert catalog["production_gpu"] and catalog["gpu_registration"] == config.pin()


def test_gpu_mounts_preserve_single_ledger_and_readonly_configuration(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "gbar_deploy", ROOT / "scripts/deploy_gbar_production.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = Path("/project")
    service = {
        "environment": {"SCAT_GBAR_V23_ENABLED": "false"},
        "volumes": [{"target": "/state", "source": "/state"}],
    }
    module.configure_gpu(service, root)
    module.configure_gpu(service, root)
    assert service["environment"]["SCAT_GPU_CONFIG_FILE"] == "var/gpu/production.json"
    assert service["environment"]["SCAT_GBAR_V23_ENABLED"] == "false"
    assert len(service["volumes"]) == 3
    ledger, config = service["volumes"][-2:]
    assert ledger["source"] == ledger["target"] == str(root / "var/gpu")
    assert ledger["read_only"] is False and config["read_only"] is True
    assert config["target"] == str(root / "var/gpu/production.json")
    assert not config["bind"]["create_host_path"]
