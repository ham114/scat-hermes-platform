"""S3 numerical oracle, persisted handoff and effect-boundary acceptance."""

import asyncio
import importlib.util
import json
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from application.model_step_store import ModelStepStore, StepError, canonical, digest
from application.model_steps import OPERATIONS, ModelSteps
from scat_model_runtime import ModelTaskRequest
from scat_model_runtime.cpu_fixture import fixture_runtime
from scat_model_runtime.step_codec import decode, encode
from scat_model_runtime.steps import ModelStepKernel


@pytest.fixture
def runtime(tmp_path):
    kernel, grant, package = fixture_runtime("scope-a", "run-a")
    store = ModelStepStore(tmp_path / "steps.sqlite", workspace_root=tmp_path)
    return ModelSteps(kernel, store, grant), package


async def advance(app, stop="scat_model_save_result"):
    args = {}
    for operation in OPERATIONS:
        result = await app.invoke(operation, args)
        args = {"reference": result["reference"]}
        if operation == stop:
            return result


@pytest.mark.asyncio
async def test_golden_cpu_matches_frozen_graph_and_saved_analysis(runtime):
    pytest.importorskip("langgraph")
    from agents.analysis_results import build_regression_model_analysis
    from agents.model_agent import build_model_agent

    app, package = runtime
    kernel = app.kernel
    graph = build_model_agent(**vars(kernel))
    request = ModelTaskRequest.model_validate_json(app.grant.request_json).model_copy(
        update={"confirmed": True}
    )
    oracle = await graph.ainvoke({"request": request, "run_id": app.grant.run_id})
    result = await advance(app)
    body = app.store.read(app.grant.scope, app.grant.run_id, result["reference"], kind="result")
    actual = decode(body["state"])
    for field in ("regression_input", "regression_output", "regression_evaluation"):
        assert encode(actual[field]) == encode(oracle[field])
    np.testing.assert_allclose(
        actual["regression_input"].series[0].values,
        ((0, 0, 1000), (10, 20, 1005), (20, 40, 1010), (30, 60, 1015)),
        rtol=0,
        atol=1e-12,
    )
    np.testing.assert_allclose(
        actual["regression_output"].series[0].values,
        ((40, 80, 1020), (50, 100, 1025)),
        rtol=0,
        atol=1e-12,
    )
    metrics = actual["regression_evaluation"].target_errors
    np.testing.assert_allclose([m.mae for m in metrics], (1, 0.5, 0), rtol=0, atol=1e-12)
    np.testing.assert_allclose([m.rmse for m in metrics], (2**0.5, 0.5**0.5, 0), rtol=0, atol=1e-12)
    assert [m.unit for m in metrics] == ["m", "m", "m"]
    assert actual["regression_output"].series[0].time_offsets_seconds == (1, 2)
    assert body["analysis_result"] == build_regression_model_analysis(
        oracle["regression_output"], oracle["regression_evaluation"]
    ).model_dump(mode="json")
    assert (await app.invoke("scat_model_read_result", {"reference": result["reference"]}))[
        "analysis_result"
    ] == body["analysis_result"]
    assert package.calls == 2  # independent old oracle + new operations


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "test_internal_model_agent_runs_registered_components_in_order",
        "test_internal_model_agent_runs_regression_package_as_black_box",
        "test_internal_model_agent_runs_comparison_package_on_its_own_registries",
    ],
)
async def test_all_kernel_branches_match_independent_legacy_fixtures(case, monkeypatch, tmp_path):
    pytest.importorskip("langgraph")
    path = Path(__file__).with_name("test_model_agent.py")
    spec = importlib.util.spec_from_file_location("s3_legacy_oracle", path)
    module = importlib.util.module_from_spec(spec)
    import sys

    monkeypatch.setitem(sys.modules, "s3_legacy_oracle", module)
    # Numerical fixtures do not need the unrelated public agent registry (and its Redis UI tools).
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    tree.body = [
        node
        for node in tree.body
        if not (isinstance(node, ast.ImportFrom) and node.module == "agents.agents")
    ]
    exec(compile(tree, str(path), "exec"), module.__dict__)  # noqa: S102 -- checked-in legacy test fixture, no untrusted input
    original = module.build_model_agent
    captured = {}

    def capture(**kwargs):
        graph = original(**kwargs)
        captured["registries"] = kwargs

        class Proxy:
            async def ainvoke(self, state):
                captured["input"] = state
                captured["output"] = await graph.ainvoke(state)
                return captured["output"]

        return Proxy()

    monkeypatch.setattr(module, "build_model_agent", capture)
    await getattr(module, case)()
    kernel = ModelStepKernel(**captured["registries"])
    state = {**captured["input"], "run_id": captured["output"]["run_id"]}
    for method in (
        kernel.validate_request,
        kernel.resolve_components,
        kernel.resolve_input,
        kernel.prepare_features,
        kernel.execute_model,
        kernel.evaluate_model,
    ):
        patch = method(state)
        if asyncio.iscoroutine(patch):
            patch = await patch
        ModelSteps._update(state, patch)
    for key in (
        "source",
        "prepared_input",
        "prediction",
        "evaluation",
        "regression_input",
        "regression_output",
        "regression_evaluation",
        "comparison_input",
        "comparison_output",
    ):
        if key in captured["output"]:
            assert encode(state[key]) == encode(captured["output"][key])
    # Exercise persisted handoff as well as the in-memory numerical kernel.
    from application.model_steps import ModelStepGrant, implementation_snapshot, spec_digest

    if "comparison_input" in state:
        # The old test package checks object identity. Its mathematical input is unchanged by JSON.
        package = kernel.comparison_model_packages.resolve(state["model_spec"].model_package)
        original_run = package.run
        source = state["comparison_input"]

        def persisted_run(*, model_input, parameters):
            assert model_input == source
            return original_run(model_input=source, parameters=parameters)

        monkeypatch.setattr(package, "run", persisted_run)
    request = captured["input"]["request"].model_copy(
        update={"confirmed": False, "model_version": state["model_spec"].version}
    )
    grant = ModelStepGrant(
        "oracle-scope",
        "oracle-run",
        canonical(request.model_dump(mode="json")),
        spec_digest(state["model_spec"]),
        time.time() + 60,
        True,
        implementation_snapshot(kernel),
    )
    service = ModelSteps(
        kernel, ModelStepStore(tmp_path / "parity.sqlite", workspace_root=tmp_path), grant
    )
    saved = await advance(service)
    result = await service.invoke("scat_model_read_result", {"reference": saved["reference"]})
    assert result["analysis_result"] == ModelSteps.analysis(captured["output"])


@pytest.mark.asyncio
async def test_receipts_reused_across_service_restart(runtime):
    app, package = runtime
    first = await advance(app, "scat_model_prepare_input")
    arguments = {"reference": first["reference"]}
    result = await app.invoke("scat_model_run", arguments)
    restored = ModelSteps(
        app.kernel, ModelStepStore(app.store.path, workspace_root=app.store.path.parent), app.grant
    )
    repeated = await restored.invoke("scat_model_run", arguments)
    assert repeated["reference"] == result["reference"] and repeated["reused"]
    assert package.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation,code",
    [
        ("scope", "step_artifact_not_found"),
        ("run", "step_artifact_not_found"),
        ("sha", "step_artifact_integrity_failed"),
        ("kind", "step_artifact_kind_mismatch"),
        ("tamper", "step_artifact_integrity_failed"),
        ("expired", "step_artifact_expired"),
        ("protocol", "step_artifact_protocol_mismatch"),
        ("lineage", "step_artifact_lineage_mismatch"),
    ],
)
async def test_handoff_rejects_invalid_reference_without_execution(runtime, mutation, code):
    app, package = runtime
    ref = (await app.invoke("scat_model_prepare_input", {}))["reference"]
    if mutation in {"scope", "run"}:
        app.grant = replace(
            app.grant, **({"scope": "scope-b"} if mutation == "scope" else {"run_id": "run-b"})
        )
    elif mutation == "sha":
        ref["content_sha256"] = "0" * 64
    elif mutation == "kind":
        ref["kind"] = "prediction"
    else:
        with sqlite3.connect(app.store.path) as db:
            if mutation == "expired":
                db.execute("UPDATE model_step_artifacts SET expires=0")
            else:
                body = json.loads(db.execute("SELECT body FROM model_step_artifacts").fetchone()[0])
                body["schema_version" if mutation == "protocol" else "request_sha256"] = "changed"
                if mutation in {"protocol", "lineage"}:
                    ref["content_sha256"] = digest(body)
                    db.execute(
                        "UPDATE model_step_artifacts SET body=?,sha=?",
                        (canonical(body), digest(body)),
                    )
                else:
                    db.execute("UPDATE model_step_artifacts SET body=?", (canonical(body),))
    with pytest.raises(StepError, match=code):
        await app.invoke("scat_model_run", {"reference": ref})
    assert package.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation,code",
    [
        ("expired", "step_grant_expired"),
        ("ordinary", "approved_job_required_s4"),
        ("spec", "model_registration_changed"),
        ("component", "component_implementation_changed"),
        ("confirmed", "invalid_step_grant"),
    ],
)
async def test_grant_and_registration_checked_before_effect(runtime, mutation, code):
    app, package = runtime
    if mutation == "expired":
        app.grant = replace(app.grant, expires=time.time() - 1)
    elif mutation == "ordinary":
        app.grant = replace(app.grant, synthetic_cpu=False)
    elif mutation == "spec":
        app.grant = replace(app.grant, spec_sha256="0" * 64)
    elif mutation == "component":
        app.grant = replace(app.grant, components_sha256="0" * 64)
    else:
        request = json.loads(app.grant.request_json)
        request["confirmed"] = True
        app.grant = replace(app.grant, request_json=canonical(request))
    with pytest.raises(StepError, match=code):
        await app.invoke("scat_model_prepare_input", {})
    assert package.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation,code",
    [
        ("unit", "model_output_unit_mismatch"),
        ("identity", "model_output_identity_mismatch"),
        ("origin", "model_output_time_or_reference_mismatch"),
    ],
)
async def test_prediction_contract_rejects_numerical_semantic_drift(
    runtime, mutation, code, monkeypatch
):
    app, package = runtime
    ref = (await app.invoke("scat_model_prepare_input", {}))["reference"]
    original = package.run

    def bad(**kwargs):
        out = original(**kwargs)
        if mutation == "unit":
            return out.model_copy(update={"target_units": {"x": "km", "y": "m", "alt": "m"}})
        if mutation == "identity":
            return out.model_copy(update={"model_version": "other"})
        from datetime import timedelta

        return out.model_copy(
            update={
                "series": (
                    out.series[0].model_copy(
                        update={
                            "forecast_origin": out.series[0].forecast_origin + timedelta(seconds=1)
                        }
                    ),
                )
            }
        )

    monkeypatch.setattr(package, "run", bad)
    with pytest.raises(StepError, match=code):
        await app.invoke("scat_model_run", {"reference": ref})
    with pytest.raises(StepError, match="step_unknown"):
        await app.invoke("scat_model_run", {"reference": ref})
    assert package.calls == 1


@pytest.mark.asyncio
async def test_lost_completion_never_reexecutes(runtime, monkeypatch):
    app, package = runtime
    ref = (await app.invoke("scat_model_prepare_input", {}))["reference"]

    def lost(*args, **kwargs):
        raise OSError("synthetic commit loss")

    monkeypatch.setattr(app.store, "save", lost)
    with pytest.raises(OSError):
        await app.invoke("scat_model_run", {"reference": ref})
    with pytest.raises(StepError, match="step_unknown"):
        await app.invoke("scat_model_run", {"reference": ref})
    assert package.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"confirmed": True},
        {"model_key": "other"},
        {"scope": "other"},
        {"reference": {}},
        {"shell": "x"},
    ],
)
async def test_tool_arguments_cannot_expand_grant(runtime, arguments):
    app, package = runtime
    with pytest.raises(StepError, match="invalid_model_step_arguments"):
        await app.invoke("scat_model_prepare_input", arguments)
    assert package.calls == 0


@pytest.mark.parametrize(
    "payload", [object(), float("nan"), float("inf"), {"$model": "os"}, np.array([object()])]
)
def test_codec_refuses_executable_or_nonfinite_payloads(payload):
    with pytest.raises(ValueError):
        encode(payload)


def test_codec_retains_tensor_dtype_shape_and_values():
    data = np.array([[1, 2], [3, 4]], dtype="float32")
    out = decode(encode(data))
    assert out.dtype == data.dtype
    np.testing.assert_array_equal(out, data)


def test_new_modules_have_no_legacy_graph_or_tool_imports():
    import ast

    root = Path(__file__).resolve().parents[2]
    for path in [
        root / "src/scat_model_runtime/steps.py",
        root / "src/application/model_steps.py",
        root / "src/application/model_records.py",
        root / "src/application/model_tasks.py",
    ]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in {
                    "agents.model_agent",
                    "agents.model_tools",
                    "langgraph.graph",
                }
