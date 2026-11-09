import pytest
from pydantic import ValidationError

from workflow_runtime.catalog import describe
from workflow_runtime.contracts import TrialConfiguration


def test_catalog_reports_model_specific_parameters_and_excludes_unregistered_gpu(monkeypatch):
    monkeypatch.setattr("workflow_runtime.gpu.registration", lambda root: None)
    catalog = describe()
    models = {m["model_id"]: m for m in catalog["models"]}
    assert set(models) == set(catalog["model_ids"]) == {"constant-velocity-heading", "kalman-cv"}
    assert set(models["constant-velocity-heading"]["parameters"]) == {"window_s"}
    assert set(models["kalman-cv"]["parameters"]) == {"process_noise_mps2", "measurement_noise_m"}
    for model in models.values():
        params = {k: v.get("default", v.get("minimum", 1)) for k, v in model["parameters"].items()}
        assert TrialConfiguration(model=model["model_id"], version=model["version"], **params)
    with pytest.raises(ValidationError):
        TrialConfiguration(model="kalman-cv", window_s=60)
    with pytest.raises(ValidationError):
        TrialConfiguration(model="constant-velocity-heading", process_noise_mps2=1)


def test_gpu_is_listed_only_after_registration_and_has_no_parameter_overrides(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "workflow_runtime.gpu.registration",
        lambda root: SimpleNamespace(pin=lambda: {"test": True}, max_windows_per_job=16),
    )
    catalog = describe()
    assert catalog["production_gpu"]
    model = catalog["models"][-1]
    assert {k: v for k, v in model.items() if k != "constraints"} == {
        "model_id": "scat-gbar-v23",
        "version": "23",
        "execution": "gpu",
        "parameters": {},
    }
    assert model["constraints"]["max_windows_per_job"] == 16
