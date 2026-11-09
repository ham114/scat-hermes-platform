import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from application.context import ExecutionContext
from application.skill_synthetic import build_synthetic_skill_application
from hermes_runtime.readonly_contracts import resolve_reference, result_reference
from hermes_runtime.results import ResultNotFoundError, ToolResultStore
from hermes_runtime.skill_authoring import validate_draft
from hermes_runtime.skill_evaluation import evaluate_cases, offline_bridge
from hermes_runtime.skill_policy import CompositionPolicy, SkillPolicy
from hermes_runtime.skill_templates import load_investigation
from hermes_runtime.skills import SkillRelease


@pytest.fixture
def workspace(tmp_path):
    source = Path(__file__).resolve().parents[2]
    for folder in ["skills", "authoring", "config", "evaluations"]:
        shutil.copytree(source / folder, tmp_path / folder)
    dest = tmp_path / "services/hermes"
    dest.mkdir(parents=True)
    shutil.copyfile(source / "services/hermes/skills.lock.json", dest / "skills.lock.json")
    release = SkillRelease(tmp_path, dest / "skills.lock.json")
    return tmp_path, release


def test_template_reuse_has_different_parameters_and_selective_branches(workspace):
    root, release = workspace
    a = load_investigation(root / "config/investigation-synthetic-a.json").prepare(
        release, SkillPolicy()
    )
    b = load_investigation(root / "config/investigation-synthetic-b.json").prepare(
        release, SkillPolicy()
    )
    assert len(a["selected_skills"]) == 5 and len(b["selected_skills"]) == 3
    assert "SYNTH-B" in b["prompt"] and "TEST456" in b["prompt"]
    assert b["effective_budget"]["max_tool_calls"] == 16
    template = load_investigation(root / "config/investigation-synthetic-a.json")
    with pytest.raises(ValueError, match="version_mismatch"):
        template.model_copy(update={"skill_version": "missing"}).prepare(release, SkillPolicy())
    with pytest.raises(ValueError, match="ceiling"):
        template.model_copy(update={"budget": {"max_tool_calls": 1000}}).prepare(
            release, SkillPolicy()
        )


def test_composition_depth_configuration_rejects_dependency_tree(workspace):
    root, release = workspace
    with pytest.raises(ValueError, match="composition_depth"):
        offline_bridge(
            release, root / "home", SkillPolicy(composition=CompositionPolicy(max_depth=1))
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("contract", "scat.trajectory-observation.v1"),
        ("bundle_sha256", "changed"),
        ("payload_sha256", "changed"),
        ("query_sha256", "changed"),
        ("owner_scope", "scat:" + "0" * 64),
        ("producer_run", "run_other"),
        ("units_protocol", "feet"),
        ("schema_version", 2),
    ],
)
def test_wrong_scope_contract_units_version_or_digest_cannot_reuse_result(workspace, field, value):
    root, release = workspace
    context = ExecutionContext("one", "thread")
    store = ToolResultStore(root / "results.sqlite", workspace_root=root)
    ref = store.put(
        context.scope_key,
        "run_one",
        "call",
        "search_scat_flights",
        {"status": "ok", "query": {"batch": "SYNTH"}, "data": {"items": []}},
    )
    typed = result_reference(store, release, context.scope_key, "run_one", ref["result_id"])
    assert (
        resolve_reference(
            store,
            release,
            context.scope_key,
            "run_one",
            typed.model_dump(),
            expected_contract=typed.contract,
        )["status"]
        == "ok"
    )
    changed = {**typed.model_dump(), field: value}
    with pytest.raises(ValueError):
        resolve_reference(
            store, release, context.scope_key, "run_one", changed, expected_contract=typed.contract
        )


def test_expired_and_tampered_receipt_cannot_be_used(workspace):
    root, release = workspace
    context = ExecutionContext("one", "thread")
    store = ToolResultStore(root / "results.sqlite", workspace_root=root)
    ref = store.put(
        context.scope_key, "run_one", "call", "search_scat_flights", {"status": "ok", "data": {}}
    )
    typed = result_reference(store, release, context.scope_key, "run_one", ref["result_id"])
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE tool_results SET payload='tampered'")
    with pytest.raises(ValueError, match="integrity"):
        resolve_reference(
            store,
            release,
            context.scope_key,
            "run_one",
            typed.model_dump(),
            expected_contract=typed.contract,
        )
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE tool_results SET expires_at=0")
    with pytest.raises(ResultNotFoundError):
        resolve_reference(
            store,
            release,
            context.scope_key,
            "run_one",
            typed.model_dump(),
            expected_contract=typed.contract,
        )


def test_draft_is_separate_hashed_and_cannot_add_unregistered_tools(workspace):
    root, release = workspace
    draft = root / "authoring/drafts/scat-investigation-candidate"
    original = validate_draft(root, draft, release)
    assert not original["published"] and not original["executed"]
    assert len(list((root / "authoring/templates").glob("*.md"))) == 11
    body = draft / "SKILL.md"
    body.write_text(body.read_text(encoding="utf-8") + "\n修订说明\n", encoding="utf-8")
    assert validate_draft(root, draft, release)["files"] != original["files"]
    meta = json.loads((draft / "draft.json").read_text())
    meta["allowed_operations"] = ["terminal"]
    (draft / "draft.json").write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="unregistered"):
        validate_draft(root, draft, release)
    assert (
        SkillRelease(root, root / "services/hermes/skills.lock.json").bundle_sha256
        == release.bundle_sha256
    )


def test_all_five_domain_contract_scenarios(workspace):
    root, _ = workspace
    report = evaluate_cases(root, root / "scenario-home")
    assert report["status"] == "passed"
    assert len(report["cases"]) == 25
    assert all(domain == {"passed": 5, "total": 5} for domain in report["domains"].values())


@pytest.mark.parametrize("field,value", [("name", "../escape"), ("version", "")])
def test_offline_draft_rejects_invalid_identity_even_with_matching_frontmatter(
    workspace, field, value
):
    root, release = workspace
    draft = root / "authoring/drafts/scat-investigation-candidate"
    meta = json.loads((draft / "draft.json").read_text())
    meta[field] = value
    (draft / "draft.json").write_text(json.dumps(meta))
    if field == "name":
        body = draft / "SKILL.md"
        body.write_text(
            body.read_text(encoding="utf-8").replace(
                "name: scat-investigation-candidate", "name: " + value
            ),
            encoding="utf-8",
        )
    with pytest.raises(ValueError, match="invalid_draft_identity"):
        validate_draft(root, draft, release)


@pytest.mark.asyncio
async def test_template_range_change_produces_new_synthetic_query_and_track(workspace):
    from application.context import ExecutionContext

    app = build_synthetic_skill_application()
    context = ExecutionContext("scat-researcher", "reuse")
    for batch, callsign in [("SYNTH", "TEST123"), ("SYNTH-B", "TEST456")]:
        result = await app.invoke(
            "search_scat_flights",
            {"batch": batch, "callsign": callsign, "limit": 1},
            context=context,
        )
        source = result["data"]["items"][0]["source"]
        assert source["batch"] == batch
        assert result["data"]["items"][0]["identity"]["callsign"] == callsign
        track = await app.invoke("get_scat_track_points", {**source, "limit": 2}, context=context)
        assert track["query"]["external_id"] == source["external_id"]
