import copy
import multiprocessing
import shutil
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes_runtime.authoring_store import AuthoringError, AuthoringStore
from hermes_runtime.release_store import ReleaseStore, planning_release
from hermes_runtime.skill_authoring import SECTIONS
from hermes_runtime.skills import SkillPolicyError
from service.skill_authoring_api import create_app

ROOT = Path(__file__).resolve().parents[2]


def test_catalog_is_authenticated_and_can_seed_editable_draft(store):
    app = create_app(
        store.root, author_key="a" * 40, release_key="b" * 40, runner=lambda *args: None
    )
    with TestClient(app) as client:
        assert client.get("/v1/skills/catalog").status_code == 401
        response = client.get("/v1/skills/catalog", headers={"X-SCAT-Author-Key": "a" * 40})
        assert response.status_code == 200
        data = response.json()
        assert data["bundle_sha256"] == store.releases.current().bundle_sha256
        assert data["items"]
        entry = next(
            item
            for item in data["items"]
            if not any(item["name"] in other["requires_skills"] for other in data["items"])
        )
        assert set(entry) == {
            "name",
            "version",
            "allowed_operations",
            "requires_skills",
            "contract",
            "files",
        }
        assert "SKILL.md" in entry["files"]
        assert isinstance(entry["requires_skills"], dict)
        entry["version"] += ".ui-test"
        created = client.post(
            "/v1/skills/drafts",
            headers={"X-SCAT-Author-Key": "a" * 40},
            json={"base": data["bundle_sha256"], "content": {"changes": [entry]}},
        )
        assert created.status_code == 201, created.text


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("SCAT_SKILL_RELEASE_STATE", raising=False)
    for folder in ("skills", "services/hermes", "config"):
        shutil.copytree(ROOT / folder, tmp_path / folder)
    monkeypatch.setattr(AuthoringStore, "source_sha", lambda self: "reviewed-source-v1")
    return AuthoringStore(tmp_path)


def content(name="author-query"):
    body = "---\nname: " + name + "\ndescription: 合成查询候选\n---\n\n"
    body += "\n".join(
        "## " + section + "\n\n使用已注册查询，依据实际结果完成；无数据停止。\n"
        for section in SECTIONS
    )
    return {
        "changes": [
            {
                "name": name,
                "version": "1.0.0",
                "allowed_operations": ["mcp__scat__search_scat_flights"],
                "requires_skills": {},
                "contract": {
                    "version": 1,
                    "inputs": ["scat.request.v1"],
                    "outputs": ["scat.query-result.v1"],
                },
                "files": {"SKILL.md": body},
            }
        ]
    }


def qualified(store, actor="owner"):
    row = store.write(actor, content())
    trial = store.begin_trial(actor, row["draft"])
    store.finish_trial(
        trial["id"],
        {
            "status": "passed",
            "bundle_sha256": trial["candidate"],
            "synthetic_only": True,
            "temporary_home_removed": True,
            "owned_processes_remaining": 0,
        },
    )
    return row, trial


def test_revision_history_scope_and_diff(store):
    row = store.write("owner", content())
    assert store.list("other") == []
    with pytest.raises(AuthoringError, match="resource_not_found"):
        store.get("other", row["draft"])
    updated = content()
    updated["changes"][0]["files"]["SKILL.md"] += "修改流程\n"
    newer = store.write("owner", updated, draft=row["draft"], expected=row["sha"])
    assert newer["revision"] == 2
    assert len(store.history("owner", row["draft"])) == 2
    assert store.get("owner", row["draft"], 1)["sha"] == row["sha"]
    assert "修改流程" in store.diff("owner", row["draft"])["files"][0]["diff"]
    with pytest.raises(AuthoringError, match="revision_conflict"):
        store.write("owner", content(), draft=row["draft"], expected=row["sha"])


def test_release_install_keeps_atomic_rename_on_destination_mount(store, monkeypatch):
    release_store = ReleaseStore(store.root, "var/container-mount/catalog.sqlite")
    baseline = release_store.baseline()
    renames = []
    original = Path.rename

    def same_mount(source, target):
        assert source.parent == target.parent == release_store.bundles
        renames.append((source, target))
        return original(source, target)

    monkeypatch.setattr(Path, "rename", same_mount)
    release_store.initialize()
    assert release_store.current().bundle_sha256 == baseline.bundle_sha256
    assert renames and not list(release_store.bundles.glob(".install-*"))


@pytest.mark.parametrize(
    "mutation",
    [
        "path",
        "script",
        "dependency",
        "cycle",
        "operation",
        "header",
        "contract",
        "size",
        "sections",
        "duplicate",
        "version",
    ],
)
def test_invalid_candidates_fail_closed(store, mutation):
    payload = content()
    change = payload["changes"][0]
    if mutation == "path":
        change["files"]["../.env"] = "bad"
    elif mutation == "script":
        change["files"]["scripts/run.py"] = "pass"
    elif mutation == "dependency":
        change["requires_skills"] = {"scat-flight-query": "wrong"}
    elif mutation == "cycle":
        change["requires_skills"] = {"author-query": "1.0.0"}
    elif mutation == "operation":
        change["allowed_operations"] = ["terminal", "skill_manage"]
    elif mutation == "header":
        change["files"]["SKILL.md"] = change["files"]["SKILL.md"].replace(
            "description:", "deps: [install-me]\ndescription:"
        )
    elif mutation == "contract":
        change["contract"]["outputs"] = []
    elif mutation == "size":
        change["files"]["references/large.md"] = "a" * 65537
    elif mutation == "sections":
        change["files"]["SKILL.md"] = change["files"]["SKILL.md"].replace("## 评测", "缺少章节")
    elif mutation == "duplicate":
        payload["changes"].append(copy.deepcopy(change))
    else:
        old = store.releases.baseline().entries["scat-flight-query"]
        change["name"], change["version"] = old["name"], old["version"]
    row = store.write("owner", payload)
    assert not store.validate("owner", row["draft"])["valid"]
    assert store.releases.current().bundle_sha256 == store.releases.baseline().bundle_sha256


def test_update_leaf_requires_dependent_version_repairs(store):
    baseline = store.releases.baseline()
    changes = []
    for name in ("scat-flight-query", "scat-investigation"):
        entry = {k: v for k, v in baseline.entries[name].items() if k != "description"}
        entry["version"] = "0.3.0"
        entry["files"] = {
            p.split("/", 1)[1]: data.decode()
            for p, data in baseline.files.items()
            if p.startswith(name + "/")
        }
        # Existing published skills may predate the author template: require explicit sections in the update.
        entry["files"]["SKILL.md"] += "\n" + "\n".join(
            "## " + s + "\n见上述已审阅流程。" for s in SECTIONS
        )
        if name == "scat-investigation":
            entry["requires_skills"]["scat-flight-query"] = "0.3.0"
        changes.append(entry)
    row = store.write("owner", {"changes": changes[:1]})
    assert not store.validate("owner", row["draft"])["valid"]
    row = store.write("owner", {"changes": changes})
    assert store.validate("owner", row["draft"])["valid"]


def test_publish_rollback_pins_old_runs_and_replay_is_idempotent(store):
    old = store.releases.pin("scope", "running")
    row, trial = qualified(store)
    preview = store.preview("owner", draft=row["draft"], trial=trial["id"])
    with pytest.raises(AuthoringError, match="human_confirmation"):
        store.confirm("owner", preview["id"], preview["sha256"], confirmed=False)
    result = store.confirm("owner", preview["id"], preview["sha256"], confirmed=True)
    assert result["target"] == trial["candidate"]
    assert store.releases.pin("scope", "running").bundle_sha256 == old.bundle_sha256
    assert planning_release(store.root, "scope", "new-run").bundle_sha256 == trial["candidate"]
    assert store.confirm("owner", preview["id"], preview["sha256"], confirmed=True)["replayed"]
    rollback = store.preview("owner", rollback=old.bundle_sha256)
    store.confirm("owner", rollback["id"], rollback["sha256"], confirmed=True)
    assert store.releases.current().bundle_sha256 == old.bundle_sha256
    assert store.releases.pin("scope", "new-run").bundle_sha256 == trial["candidate"]
    assert store.releases.pin("scope", "third-run").bundle_sha256 == old.bundle_sha256
    with closing(store.releases.connect()) as db:
        assert db.execute("SELECT count(*) FROM release_events").fetchone()[0] == 2


@pytest.mark.parametrize(
    "reason", ["draft", "source", "expiry", "aba", "tamper", "digest", "owner"]
)
def test_preview_invalidations(store, monkeypatch, reason):
    row, trial = qualified(store)
    preview = store.preview("owner", draft=row["draft"], trial=trial["id"])
    actor, sha = "owner", preview["sha256"]
    if reason == "draft":
        store.write("owner", content(), draft=row["draft"], expected=row["sha"])
    elif reason == "source":
        monkeypatch.setattr(store, "source_sha", lambda: "changed")
    elif reason == "expiry":
        monkeypatch.setattr(store, "clock", lambda: preview["expires"] + 1)
    elif reason == "aba":
        with closing(store.releases.connect()) as db, db:
            db.execute("UPDATE active SET generation=generation+2")
    elif reason == "tamper":
        path = store.releases.bundles / trial["candidate"] / "skills/author-query/SKILL.md"
        path.chmod(0o600)
        path.write_text("changed")
    elif reason == "digest":
        sha = "0" * 64
    else:
        actor = "other"
    with pytest.raises((AuthoringError, SkillPolicyError)):
        store.confirm(actor, preview["id"], sha, confirmed=True)
    assert store.releases.current().bundle_sha256 == row["base"]


def test_trial_gate_scope_interruption_and_client_report_rejection(store):
    row = store.write("owner", content())
    trial = store.begin_trial("owner", row["draft"])
    with pytest.raises(AuthoringError, match="already_running"):
        store.begin_trial("owner", row["draft"])
    with pytest.raises(AuthoringError):
        store.trial("other", trial["id"])
    with pytest.raises(AuthoringError, match="successful_trial"):
        store.preview("owner", draft=row["draft"], trial=trial["id"])
    assert store.recover_trials() == 1
    assert store.trial("owner", trial["id"])["state"] == "interrupted"
    with pytest.raises(AuthoringError, match="not_running"):
        store.finish_trial(trial["id"], {"status": "passed"})


def test_concurrent_draft_revision_has_one_winner(store):
    row = store.write("owner", content())

    def revise(_):
        try:
            store.write("owner", content(), draft=row["draft"], expected=row["sha"])
            return True
        except AuthoringError:
            return False

    with ThreadPoolExecutor(4) as pool:
        assert sum(pool.map(revise, range(4))) == 1


def _uncommitted_change(path, ready):
    db = sqlite3.connect(path)
    db.execute("BEGIN IMMEDIATE")
    db.execute("UPDATE active SET sha='not-a-release',generation=generation+1")
    ready.set()
    time.sleep(60)


def test_process_kill_before_commit_preserves_old_pointer(store):
    before = store.releases.current().bundle_sha256
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    child = context.Process(target=_uncommitted_change, args=(str(store.releases.path), ready))
    child.start()
    try:
        assert ready.wait(10)
        child.kill()
        child.join(10)
        assert store.releases.current().bundle_sha256 == before
        with closing(store.releases.connect()) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        if child.is_alive():
            child.kill()
            child.join(10)


def test_api_separates_draft_and_release_authority(store):
    author, release = "a" * 40, "r" * 40
    app = create_app(store.root, author_key=author, release_key=release, runner=lambda *args: None)
    with TestClient(app) as client:
        assert client.get("/v1/skills/drafts").status_code == 401
        headers = {"X-SCAT-Author-Key": author}
        response = client.post(
            "/v1/skills/drafts", headers=headers, json={"content": content(), "owner": "forged"}
        )
        assert response.status_code == 422
        row = client.post("/v1/skills/drafts", headers=headers, json={"content": content()}).json()
        trial = client.post(f"/v1/skills/drafts/{row['draft']}/trials", headers=headers).json()
        assert trial["state"] == "running"
        assert (
            client.put(
                f"/v1/skills/trials/{trial['id']}", headers=headers, json={"passed": True}
            ).status_code
            == 405
        )
        request = {"preview": "1" * 32, "sha256": "0" * 64, "confirmed": True}
        assert (
            client.post("/v1/skills/releases/confirm", headers=headers, json=request).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/skills/releases/confirm",
                headers=headers | {"X-SCAT-Release-Key": release},
                json=request,
            ).status_code
            == 404
        )
        assert "release_key" not in client.get("/v1/skills/releases", headers=headers).text


def test_baseline_fallback_and_unknown_release_rejection(store, monkeypatch):
    other = store.root / "fresh"
    shutil.copytree(store.root / "skills", other / "skills")
    shutil.copytree(store.root / "services", other / "services")
    fallback = ReleaseStore(other)
    assert fallback.current().bundle_sha256 == store.releases.baseline().bundle_sha256
    assert not fallback.path.exists()
    with pytest.raises(SkillPolicyError):
        fallback.resolve("0" * 64)
    monkeypatch.setenv("SCAT_SKILL_RELEASE_STATE", "../outside.sqlite")
    with pytest.raises(SkillPolicyError):
        ReleaseStore(store.root)


def test_author_subset_gate_does_not_weaken_full_s6_gate():
    from hermes_runtime.stability import summarize

    attempts = [
        {
            "domains": ["query", "quality", "trajectory"],
            "passed": True,
            "selection_parameters_passed": True,
            "elapsed_seconds": 1,
        }
        for _ in range(3)
    ]
    assert summarize(attempts)["status"] == "failed"
    expected = {"query", "quality", "trajectory"}
    assert summarize(attempts, expected_domains=expected)["status"] == "passed"
    assert summarize(attempts[:2], expected_domains=expected)["status"] == "failed"
    assert summarize(attempts, expected_domains=expected | {"knowledge"})["status"] == "failed"
    assert summarize([], expected_domains=set())["status"] == "failed"


def test_release_transaction_rolls_back_pointer_when_history_insert_fails(store):
    row, trial = qualified(store)
    preview = store.preview("owner", draft=row["draft"], trial=trial["id"])
    with closing(store.releases.connect()) as db, db:
        db.execute(
            "CREATE TRIGGER inject_failure BEFORE INSERT ON release_events BEGIN SELECT RAISE(ABORT, 'injected interruption'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.confirm("owner", preview["id"], preview["sha256"], confirmed=True)
    assert store.releases.current().bundle_sha256 == row["base"]
    with closing(store.releases.connect()) as db:
        assert (
            db.execute(
                "SELECT applied FROM release_previews WHERE id=?", (preview["id"],)
            ).fetchone()[0]
            == 0
        )


def test_concurrent_publications_require_a_fresh_preview(store):
    row, trial = qualified(store)
    previews = [store.preview("owner", draft=row["draft"], trial=trial["id"]) for _ in range(2)]

    def confirm(preview):
        try:
            store.confirm("owner", preview["id"], preview["sha256"], confirmed=True)
            return True
        except AuthoringError:
            return False

    with ThreadPoolExecutor(2) as pool:
        assert sum(pool.map(confirm, previews)) == 1


def test_confirmed_job_and_native_lease_keep_original_release_after_publication(store, monkeypatch):
    from application.context import ExecutionContext
    from application.model_tasks import TaskProposal
    from application.skill_job_factory import create_skill_tasks
    from hermes_runtime.job_context import load_approved_job
    from scat_model_runtime import ModelTaskRequest

    app = create_skill_tasks(store.root, store.root / "jobs.sqlite", cpu_fixture=True)
    monkeypatch.setattr(app.skills, "sources", dict)
    context = ExecutionContext("owner", "thread")
    _, grant = app.skills.kernel()
    request = ModelTaskRequest.model_validate_json(grant.request_json)
    proposal = TaskProposal(**{name: getattr(request, name) for name in TaskProposal.model_fields})
    view = app.prepare(context, proposal, proposal_key="original")
    app.confirm(context, view["task_id"], view["snapshot_sha256"])
    claimed = app.skills.store.claim("worker")
    baseline = claimed["snapshot"]["skills"]["bundle_sha256"]
    row, trial = qualified(store)
    preview = store.preview("owner", draft=row["draft"], trial=trial["id"])
    store.confirm("owner", preview["id"], preview["sha256"], confirmed=True)
    app.skills.validate(claimed)
    _, admitted = load_approved_job(
        store.root, app.store.path, claimed["task_id"], "worker", claimed["job"]["generation"]
    )
    assert admitted["snapshot"]["skills"]["bundle_sha256"] == baseline
    new = app.prepare(context, proposal, proposal_key="new")
    assert (
        app.skills.store.get(context.scope_key, new["task_id"])["snapshot"]["skills"][
            "bundle_sha256"
        ]
        == trial["candidate"]
    )
