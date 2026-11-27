"""Run the independent visualization worker against the trusted launcher service."""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


async def serve(once):
    from application.visualization_tasks import create_visualization_tasks
    from hermes_runtime.results import ToolResultStore
    from visualization_runtime.paths import confined_path
    from visualization_runtime.sandbox_service import SandboxClient
    from visualization_runtime.settings import load_settings
    from visualization_runtime.worker import VisualizationWorker

    settings = load_settings(ROOT)
    if not settings.sandbox_image:
        raise ValueError("SCAT_VIS_SANDBOX_IMAGE_required")
    receipts = ToolResultStore(
        confined_path(
            ROOT,
            Path(os.environ.get("HERMES_RESULT_STATE_FILE", "var/platform/hermes-results.sqlite")),
        ),
        workspace_root=ROOT,
    )
    app = create_visualization_tasks(
        ROOT,
        Path(os.environ.get("HERMES_TASK_STATE_FILE", "var/platform/model-tasks.sqlite")),
        settings,
        receipts,
    )
    key = confined_path(ROOT, Path(settings.sandbox_key_file)).read_text().strip()
    launcher = SandboxClient(
        app.jobs,
        app.snapshots,
        settings.sandbox_url,
        key,
        image=settings.sandbox_image,
        timeout=settings.render_timeout_seconds + 30,
    )
    worker = VisualizationWorker(app, launcher, settings)
    worker_id = "vis_" + uuid4().hex
    while True:
        worked = await worker.run_once(worker_id)
        if once:
            return
        if not worked:
            await asyncio.sleep(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    asyncio.run(serve(parser.parse_args().once))
