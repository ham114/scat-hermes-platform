"""Run the independent, single-concurrency model worker in Anaconda."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


async def serve(args):
    if not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use an Anaconda Python environment")
    temporary = Path(os.environ.get("SCAT_JOB_TEMP_DIR", str(ROOT / ".tmp/model-worker"))).resolve()
    if not temporary.is_relative_to(ROOT) or temporary == ROOT:
        raise RuntimeError("Worker temporary directory must remain in project")
    temporary.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary), PYTHONDONTWRITEBYTECODE="1"
    )
    from application.model_tasks import ModelTasks
    from application.model_worker import reconcile, run_once
    from application.task_store import TaskStore
    from core import settings
    from hermes_runtime.registry import RunRegistry
    from scat.infrastructure.database import flight_database
    from scat_model_runtime.production import (
        configure_production_model_runtime,
        shutdown_production_model_runtime,
    )
    from service.hermes_app import project_path

    configure_production_model_runtime(settings)
    app = ModelTasks(TaskStore(project_path(settings.HERMES_TASK_STATE_FILE), workspace_root=ROOT))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
    worker = "worker_" + uuid4().hex
    try:
        if not args.legacy_h5:
            from application.skill_job_factory import create_skill_tasks
            from application.skill_worker import BackgroundHermes, SkillWorker

            app = create_skill_tasks(
                ROOT,
                project_path(settings.HERMES_TASK_STATE_FILE),
                cpu_fixture=os.environ.get("SCAT_S4_CPU_FIXTURE") == "1",
            )
            runner = SkillWorker(
                app,
                BackgroundHermes(
                    ROOT, home_base=project_path(args.job_home), port=args.background_port
                ),
                worker=worker,
                lease_seconds=args.lease_seconds,
                heartbeat_interval=min(5, args.lease_seconds / 3),
            )
            if args.reconcile:
                registry = RunRegistry(
                    project_path(settings.HERMES_STATE_FILE), workspace_root=ROOT
                )
                context = registry.require_thread(settings.ANALYSIS_SINGLE_USER_ID, args.thread)
                app.skills.reconcile(context, args.reconcile)
                print("Skill job evidence reconciled; no direct model execution")
                return
            while not stop.is_set():
                worked = await runner.once()
                if args.once:
                    break
                if not worked:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=2)
                    except TimeoutError:
                        pass
            return
        if args.reconcile:
            registry = RunRegistry(project_path(settings.HERMES_STATE_FILE), workspace_root=ROOT)
            context = registry.require_thread(settings.ANALYSIS_SINGLE_USER_ID, args.thread)
            await reconcile(app, context, args.reconcile)
            print("Verified terminal run reconciled; no execution was started")
            return
        while not stop.is_set():
            worked = await run_once(app, worker)
            if args.once:
                break
            if not worked:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=2)
                except TimeoutError:
                    pass
    finally:
        shutdown_production_model_runtime()
        await flight_database.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--legacy-h5", action="store_true", help="Historical graph compatibility only"
    )
    parser.add_argument("--job-home", default="var/hermes/jobs")
    parser.add_argument("--background-port", type=int, default=18095)
    parser.add_argument("--lease-seconds", type=int, default=60)
    parser.add_argument("--reconcile", metavar="TASK_ID")
    parser.add_argument("--thread", metavar="THREAD_ID")
    args = parser.parse_args()
    if bool(args.reconcile) != bool(args.thread):
        parser.error("--reconcile and --thread must be supplied together")
    if not 1 <= args.lease_seconds <= 300:
        parser.error("Lease must be between 1 and 300 seconds")
    asyncio.run(serve(args))
