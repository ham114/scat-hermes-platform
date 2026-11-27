"""Trusted admission from scoped receipts to durable visualization jobs."""

from visualization_runtime.contracts import VisualizationRequest, VisualizationTaskView
from visualization_runtime.publication import Publisher
from visualization_runtime.snapshots import digest
from visualization_runtime.store import JobConflict


class VisualizationTasks:
    def __init__(
        self,
        jobs,
        snapshots,
        receipts,
        *,
        artifacts=None,
        enabled=True,
        parent_budget_path=None,
        admission_check=None,
        preferences=None,
    ):
        self.jobs, self.snapshots = jobs, snapshots
        self.receipts, self.artifacts = receipts, artifacts
        self.enabled, self.parent_budget_path = enabled, parent_budget_path
        self.admission_check = admission_check
        self.preferences = preferences
        self.publisher = Publisher(jobs, snapshots, None)

    def _admission(self, parent_run_id):
        if not self.enabled:
            raise JobConflict("autonomous_visualization_disabled")
        if self.admission_check:
            self.admission_check()
        if self.parent_budget_path is None or not parent_run_id:
            raise JobConflict("visualization_parent_budget_unavailable")
        return self.parent_budget_path, parent_run_id

    async def sources(self, context, registry):
        import asyncio
        from datetime import UTC, datetime

        refs = []
        for run in await asyncio.to_thread(registry.list_runs, context, 10):
            for ref in await asyncio.to_thread(
                self.receipts.list, context.scope_key, run["run_id"]
            ):
                refs.append(
                    {
                        "kind": "readonly_result",
                        "source_id": ref["result_id"],
                        "run_id": run["run_id"],
                        "payload_sha256": ref["payload_sha256"],
                        "operation": ref["operation"],
                    }
                )
        if self.artifacts is not None:
            records = await self.artifacts.list_for_owner(owner_user_id=context.user_id, limit=50)
            for record in records:
                if record.thread_id == context.thread_id and (
                    record.expires_at is None
                    or record.expires_at > datetime.fromtimestamp(self.jobs.clock(), UTC)
                ):
                    refs.append(
                        {
                            "kind": "analysis_artifact",
                            "source_id": record.artifact_id,
                            "run_id": record.run_id,
                            "payload_sha256": record.payload_sha256,
                            "operation": record.analysis_result.operation,
                        }
                    )
        with self.jobs.transaction() as db:
            revoked = {
                (r[0], r[1])
                for r in db.execute(
                    "SELECT kind,source_id FROM visualization_revoked_sources WHERE owner=?",
                    (context.user_id,),
                )
            }
        return {
            "items": [ref for ref in refs if (ref["kind"], ref["source_id"]) not in revoked][:100]
        }

    async def create(self, context, body, *, parent_run_id, learn=False):
        from visualization_runtime.preferences import extract

        parent = self._admission(parent_run_id)
        local = extract(body.intent)
        return await self.prepare(
            context,
            body.request_id,
            intent=body.intent,
            references=body.data_refs,
            preferences_sha256=digest(local),
            preferences=local,
            parent=parent,
            admission_sha=digest(
                body.model_dump(mode="json", exclude={"memory_epoch", "memory_revision"})
            ),
            preference_options={
                "local": local,
                "text": body.intent if learn else None,
                "token": self._memory_token(body),
                "request_id": body.request_id,
            },
        )

    @staticmethod
    def _memory_token(body):
        return (
            {"epoch": body.memory_epoch, "revision": body.memory_revision}
            if body.memory_epoch is not None and body.memory_revision is not None
            else None
        )

    def revise(self, context, artifact_id, body, *, parent_run_id, learn=False):
        import json

        from visualization_runtime.preferences import extract, setup, task_type

        parent = self._admission(parent_run_id)
        manifest = self.publisher.get(context, artifact_id)
        original = self.jobs.get(context, manifest["task_id"])["request"]
        snapshot = self.snapshots.load(context, original.snapshot_id, original.snapshot_sha256)
        with self.jobs.transaction() as db:
            binding = db.execute(
                "SELECT local_json FROM visualization_preference_bindings WHERE task_id=?",
                (manifest["task_id"],),
            ).fetchone()
        local = (json.loads(binding[0]) if binding else extract(original.intent)) | extract(
            body.feedback
        )
        intent = original.intent + "\n用户本次修改要求：" + body.feedback
        if len(intent) > 8000:
            raise JobConflict("visualization_revision_intent_too_long")
        request = original.model_copy(
            update={
                "intent": intent,
                "parent_artifact_id": artifact_id,
                "parent_version": manifest["version"],
                "preferences": local,
                "preferences_sha256": digest(local),
                "preferences_round": 0,
            }
        )
        preference_setup = (
            setup(
                self.preferences,
                context,
                local,
                task_type(snapshot),
                text=body.feedback if learn else None,
                token=self._memory_token(body),
                source=artifact_id,
                request_id=body.request_id,
            )
            if self.preferences
            else None
        )
        return self.jobs.create(
            context,
            body.request_id,
            request,
            parent=parent,
            admission_sha=digest(
                {
                    "body": body.model_dump(
                        mode="json", exclude={"memory_epoch", "memory_revision", "artifact_id"}
                    ),
                    "artifact": artifact_id,
                }
            ),
            preference_setup=preference_setup,
        )

    def view(self, context, task_id):
        job = self.jobs.get(context, task_id)
        request = job["request"]
        with self.jobs.transaction() as db:
            row = db.execute(
                "SELECT artifact_id FROM visualization_publications WHERE task_id=?", (task_id,)
            ).fetchone()
        return VisualizationTaskView(
            task_id=task_id,
            state=job["state"],
            phase=job["phase"],
            intent=request.intent,
            created_at=job["created"],
            error_code=job["error_code"],
            artifact_id=row[0] if row else None,
            parent_artifact_id=request.parent_artifact_id,
            version=(request.parent_version or 0) + 1,
        )

    def list(self, context):
        with self.jobs.transaction() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT task_id FROM visualization_jobs WHERE owner=? AND thread=? ORDER BY created DESC,task_id DESC LIMIT 100",
                    (context.user_id, context.thread_id),
                )
            ]
        return {"enabled": self.enabled, "items": [self.view(context, i) for i in ids]}

    def artifact_view(self, context, artifact_id):
        manifest = self.publisher.get(context, artifact_id)
        job = self.jobs.get(context, manifest["task_id"])
        from schema.analysis import AnalysisResult, VisualizationRefBlock
        from visualization_runtime.contracts import VisualizationArtifactView

        return VisualizationArtifactView(
            **{
                k: manifest[k]
                for k in (
                    "artifact_id",
                    "task_id",
                    "version",
                    "snapshot_sha256",
                    "source_sha256",
                    "review_id",
                    "files",
                )
            },
            intent=job["request"].intent,
            created_at=manifest.get("created_at", job["updated"]),
            parent_artifact_id=job["request"].parent_artifact_id,
            viewports=job["request"].viewports,
            data_refs=manifest.get("data_refs", []),
            analysis_result=AnalysisResult(
                schema_version="1.7",
                dataset="cross_dataset",
                operation="autonomous_visualization",
                title="自主图表",
                blocks=[
                    VisualizationRefBlock(
                        title="已审核图表",
                        artifact_id=artifact_id,
                        thread_id=context.thread_id,
                        version=manifest["version"],
                    )
                ],
            ),
        )

    async def prepare(
        self,
        context,
        request_id,
        *,
        intent,
        references,
        preferences_sha256,
        preferences=None,
        parent=None,
        admission_sha=None,
        preference_options=None,
    ):
        snapshot = await self.snapshots.capture(
            context, request_id, references, receipts=self.receipts, artifacts=self.artifacts
        )
        request = VisualizationRequest(
            intent=intent,
            snapshot_id=snapshot["snapshot_id"],
            snapshot_sha256=snapshot["payload_sha256"],
            preferences_sha256=preferences_sha256,
            preferences=preferences,
        )
        from visualization_runtime.preferences import setup, task_type

        preference_setup = (
            setup(self.preferences, context, kind=task_type(snapshot), **preference_options)
            if self.preferences and preference_options is not None
            else None
        )
        return self.jobs.create(
            context,
            request_id,
            request,
            parent=parent,
            admission_sha=admission_sha,
            preference_setup=preference_setup,
        )

    def require_snapshot(self, context, task_id):
        job = self.jobs.get(context, task_id)
        request = job["request"]
        return self.snapshots.load(context, request.snapshot_id, request.snapshot_sha256)


def create_visualization_tasks(
    root,
    task_state,
    settings,
    receipts,
    *,
    artifacts=None,
    parent_budget_path=None,
    read_only=False,
    preferences=None,
):
    from visualization_runtime.settings import ConfigurationError
    from visualization_runtime.snapshots import SnapshotStore
    from visualization_runtime.store import VisualizationJobStore

    if not settings.enabled and not read_only:
        raise ConfigurationError("autonomous_visualization_disabled")
    if settings.enabled and not read_only:
        settings.provider("code")
        settings.provider("review")

    def admission_check():
        settings.provider("code")
        settings.provider("review")
        if not settings.sandbox_image:
            raise ConfigurationError("SCAT_VIS_SANDBOX_IMAGE_required")

    jobs = VisualizationJobStore(
        task_state, workspace_root=root, limits=settings.execution_limits()
    )
    snapshots = SnapshotStore(jobs, root / settings.artifact_root, workspace_root=root)
    if preferences is None:
        from visualization_runtime.preferences import configured

        preferences = configured(root)
    return VisualizationTasks(
        jobs,
        snapshots,
        receipts,
        artifacts=artifacts,
        enabled=settings.enabled,
        parent_budget_path=parent_budget_path,
        admission_check=admission_check,
        preferences=preferences,
    )
