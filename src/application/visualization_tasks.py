"""Trusted admission from scoped receipts to durable visualization jobs."""

from visualization_runtime.contracts import VisualizationRequest


class VisualizationTasks:
    def __init__(self, jobs, snapshots, receipts, *, artifacts=None):
        self.jobs, self.snapshots = jobs, snapshots
        self.receipts, self.artifacts = receipts, artifacts

    async def prepare(self, context, request_id, *, intent, references, preferences_sha256):
        snapshot = await self.snapshots.capture(
            context, request_id, references, receipts=self.receipts, artifacts=self.artifacts
        )
        request = VisualizationRequest(
            intent=intent,
            snapshot_id=snapshot["snapshot_id"],
            snapshot_sha256=snapshot["payload_sha256"],
            preferences_sha256=preferences_sha256,
        )
        return self.jobs.create(context, request_id, request)

    def require_snapshot(self, context, task_id):
        job = self.jobs.get(context, task_id)
        request = job["request"]
        return self.snapshots.load(context, request.snapshot_id, request.snapshot_sha256)


def create_visualization_tasks(root, task_state, settings, receipts, *, artifacts=None):
    from visualization_runtime.settings import ConfigurationError
    from visualization_runtime.snapshots import SnapshotStore
    from visualization_runtime.store import VisualizationJobStore

    if not settings.enabled:
        raise ConfigurationError("autonomous_visualization_disabled")
    settings.provider("code")
    settings.provider("review")
    jobs = VisualizationJobStore(
        task_state, workspace_root=root, limits=settings.execution_limits()
    )
    snapshots = SnapshotStore(jobs, root / settings.artifact_root, workspace_root=root)
    return VisualizationTasks(jobs, snapshots, receipts, artifacts=artifacts)
