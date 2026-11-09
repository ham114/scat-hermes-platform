"""Private author surface. Business and author LLMs never receive release credentials."""

from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from hermes_runtime.authoring_store import AuthoringError, AuthoringStore
from hermes_runtime.skill_learning import SkillLearningStore
from hermes_runtime.skills import SkillPolicyError, checked_path


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DraftBody(Body):
    content: dict
    expected_sha256: str | None = None
    base: str | None = None


class LearningEvidence(Body):
    kind: Literal["honcho_memory", "task_review"]
    reference: str = Field(min_length=1, max_length=256)
    revision: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=4096)


class LearningBody(Body):
    purpose: str = Field(min_length=1, max_length=2048)
    evidence: list[LearningEvidence] = Field(min_length=1, max_length=8)


class LearningDraftBody(Body):
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content: dict


class PreviewBody(Body):
    draft: str | None = None
    trial: str | None = None
    rollback: str | None = None


class Confirmation(Body):
    preview: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: bool


def private_key(root, filename):
    path = checked_path(root, filename)
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise RuntimeError("Author credential must have mode 0600")
    value = path.read_text().strip()
    if len(value) < 32:
        raise RuntimeError("Independent author credentials required")
    return value


def create_app(root, *, state=None, author_key, release_key, actor="scat-researcher", runner=None):
    root = Path(root).resolve()
    if (
        len(author_key) < 32
        or len(release_key) < 32
        or secrets.compare_digest(author_key, release_key)
    ):
        raise RuntimeError("Distinct author and release credentials required")
    store = AuthoringStore(root, state)
    learning = SkillLearningStore(store)
    children = {}
    lock = None

    def authorize(request: Request, x_scat_author_key: str = Header(default="")):
        if not secrets.compare_digest(x_scat_author_key, author_key):
            raise HTTPException(401, "author_authentication_required")
        if request.headers.get("x-portal-subject"):
            from core import settings
            from hermes_runtime.portal_identity import request_principal

            try:
                principal = request_principal(
                    request, settings.PORTAL_IDENTITIES_FILE, settings.PORTAL_SIGNING_KEY_FILE
                )
            except (ValueError, OSError, TypeError):
                raise HTTPException(401, "Invalid portal identity") from None
            if principal["role"] != "admin":
                raise HTTPException(403, "Administrator required")
            return principal["owner_id"]
        return actor

    def release_authorize(x_scat_release_key: str = Header(default="")):
        if not secrets.compare_digest(x_scat_release_key, release_key):
            raise HTTPException(403, "independent_release_confirmation_required")

    owner_dependency = Depends(authorize)

    @asynccontextmanager
    async def lifespan(app):
        nonlocal lock
        if runner is None:
            import fcntl

            lock = (store.releases.path.parent / "author.lock").open("a")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                lock.close()
                raise RuntimeError("Another author service or trial is active") from None
            from hermes_runtime.author_trials import recover_interrupted

            await recover_interrupted(store)
        try:
            yield
        finally:
            for child in children.values():
                if child.poll() is None:
                    child.terminate()
            for child in children.values():
                if child.poll() is None:
                    try:
                        await asyncio.to_thread(child.wait, timeout=30)
                    except subprocess.TimeoutExpired:
                        # The inherited lock keeps the next service from launching a
                        # second trial until this owned worker has completed cleanup.
                        pass
            if lock:
                lock.close()

    app = FastAPI(
        title="SCAT Skill Authoring",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.authoring_store = store
    app.state.skill_learning_store = learning

    @app.exception_handler(AuthoringError)
    @app.exception_handler(SkillPolicyError)
    async def error(request, exc):
        return JSONResponse(
            status_code=404 if str(exc) == "resource_not_found" else 409,
            content={"error": str(exc)},
        )

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/v1/skills/catalog")
    def catalog(bundle: str | None = None, owner=owner_dependency):
        release = store.releases.load(bundle) if bundle else store.releases.current()
        return {
            "bundle_sha256": release.bundle_sha256,
            "items": [
                {
                    **{
                        key: entry[key]
                        for key in (
                            "name",
                            "version",
                            "allowed_operations",
                            "requires_skills",
                            "contract",
                        )
                    },
                    "files": {
                        key[len(name) + 1 :]: value.decode("utf-8")
                        for key, value in release.files.items()
                        if key.startswith(name + "/")
                    },
                }
                for name, entry in release.entries.items()
            ],
        }

    @app.get("/v1/skills/drafts")
    def listing(owner=owner_dependency):
        return {"items": store.list(owner)}

    @app.post("/v1/skills/learning/candidates", status_code=201)
    def learning_create(body: LearningBody, owner=owner_dependency):
        return learning.create(owner, body.purpose, [item.model_dump() for item in body.evidence])

    @app.get("/v1/skills/learning/candidates")
    def learning_list(owner=owner_dependency):
        return {"items": learning.list(owner)}

    @app.get("/v1/skills/learning/candidates/{identity}")
    def learning_read(identity: str, owner=owner_dependency):
        return learning.get(owner, identity)

    @app.post("/v1/skills/learning/candidates/{identity}/draft", status_code=201)
    def learning_draft(identity: str, body: LearningDraftBody, owner=owner_dependency):
        return learning.create_draft(owner, identity, body.expected_sha256, body.content)

    @app.post("/v1/skills/drafts", status_code=201)
    def create(body: DraftBody, owner=owner_dependency):
        if body.expected_sha256 is not None:
            raise AuthoringError("unexpected_revision_precondition")
        return store.write(owner, body.content, base=body.base)

    @app.put("/v1/skills/drafts/{identity}")
    def update(identity: str, body: DraftBody, owner=owner_dependency):
        return store.write(
            owner, body.content, draft=identity, expected=body.expected_sha256, base=body.base
        )

    @app.get("/v1/skills/drafts/{identity}")
    def get(identity: str, revision: int | None = None, owner=owner_dependency):
        return store.get(owner, identity, revision)

    @app.get("/v1/skills/drafts/{identity}/history")
    def history(identity: str, owner=owner_dependency):
        return {"items": store.history(owner, identity)}

    @app.get("/v1/skills/drafts/{identity}/diff")
    def diff(identity: str, revision: int | None = None, owner=owner_dependency):
        return store.diff(owner, identity, revision)

    @app.post("/v1/skills/drafts/{identity}/validate")
    def validate(identity: str, owner=owner_dependency):
        return store.validate(owner, identity)

    @app.post("/v1/skills/drafts/{identity}/trials", status_code=202)
    def trial(identity: str, owner=owner_dependency):
        record = store.begin_trial(owner, identity)
        if runner:
            runner(store, owner, record)
        else:
            # Reap exited workers; retained reports live in SQLite and validation files.
            for key in list(children):
                if children[key].poll() is not None:
                    children.pop(key)
            directory = root / "var/validation"
            directory.mkdir(parents=True, exist_ok=True)
            try:
                with (directory / ("s7a-trial-" + record["id"] + ".log")).open("x") as log:
                    Path(log.name).chmod(0o600)
                    env = {k: v for k, v in os.environ.items() if not k.startswith("SCAT_AUTHOR_")}
                    env.update(
                        SCAT_SKILL_RELEASE_STATE=store.releases.path.relative_to(root).as_posix(),
                        PYTHONDONTWRITEBYTECODE="1",
                    )
                    temporary = root / ".tmp/s7a/worker-tmp"
                    temporary.mkdir(parents=True, exist_ok=True)
                    env.update({key: str(temporary) for key in ("TMP", "TEMP", "TMPDIR")})
                    children[record["id"]] = subprocess.Popen(
                        [
                            os.environ.get(
                                "SCAT_AGENT_PYTHON",
                                "/home/h3c/anaconda3/envs/scat-agent/bin/python",
                            ),
                            "-B",
                            str(root / "scripts/run_skill_author_trial.py"),
                            "--actor",
                            owner,
                            "--trial",
                            record["id"],
                        ],
                        cwd=root,
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        start_new_session=True,
                        pass_fds=(lock.fileno(),),
                    )
            except OSError:
                store.finish_trial(
                    record["id"], {"status": "failed", "error": "trial_launch_failed"}
                )
                raise HTTPException(503, "trial_launch_failed") from None
        return store.trial(owner, record["id"])

    @app.get("/v1/skills/trials/{identity}")
    def status(identity: str, owner=owner_dependency):
        if identity in children and children[identity].poll() is not None:
            record = store.trial(owner, identity)
            if record["state"] == "running":
                store.finish_trial(
                    identity, {"status": "failed", "error": "trial_worker_interrupted"}
                )
        return store.trial(owner, identity)

    @app.get("/v1/skills/releases")
    def releases(owner=owner_dependency):
        from contextlib import closing

        with closing(store.releases.connect()) as db:
            events = [
                dict(row)
                for row in db.execute("SELECT * FROM release_events ORDER BY id DESC LIMIT 100")
            ]
        return {"active": store.releases.current().bundle_sha256, "history": events}

    @app.post("/v1/skills/releases/preview")
    def preview(body: PreviewBody, owner=owner_dependency):
        if bool(body.rollback) == bool(body.draft and body.trial):
            raise AuthoringError("choose_publication_or_rollback")
        return store.preview(owner, draft=body.draft, trial=body.trial, rollback=body.rollback)

    @app.post("/v1/skills/releases/confirm", dependencies=[Depends(release_authorize)])
    def confirm(body: Confirmation, owner=owner_dependency):
        return store.confirm(owner, body.preview, body.sha256, confirmed=body.confirmed)

    from service.agent_settings_api import install_settings

    install_settings(app, root, authorize, release_authorize)
    return app


def configured_app():
    root = Path(__file__).resolve().parents[2]
    author = private_key(
        root, os.environ.get("SCAT_AUTHOR_KEY_FILE", "var/secrets/skill-author.key")
    )
    release = private_key(
        root, os.environ.get("SCAT_AUTHOR_RELEASE_KEY_FILE", "var/secrets/skill-release.key")
    )
    return create_app(root, author_key=author, release_key=release)
