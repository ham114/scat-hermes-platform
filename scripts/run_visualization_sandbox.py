"""Start the trusted loopback Docker launcher; no model provider credentials required."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    from visualization_runtime.launcher import RenderLauncher
    from visualization_runtime.paths import confined_path
    from visualization_runtime.sandbox import DockerSandbox
    from visualization_runtime.sandbox_service import server
    from visualization_runtime.snapshots import SnapshotStore
    from visualization_runtime.store import VisualizationJobStore

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="locally built sha256 image ID")
    parser.add_argument("--state", default="var/platform/model-tasks.sqlite")
    parser.add_argument("--artifacts", default="var/visualizations")
    parser.add_argument("--key-file", default="var/secrets/visualization-sandbox.key")
    parser.add_argument("--port", type=int, default=18166)
    parser.add_argument("--render-timeout", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()
    key_path = confined_path(ROOT, Path(args.key_file))
    if sys.platform != "win32" and key_path.stat().st_mode & 0o077:
        raise ValueError("sandbox_key_requires_0600")
    jobs = VisualizationJobStore(Path(args.state), workspace_root=ROOT)
    snapshots = SnapshotStore(jobs, Path(args.artifacts), workspace_root=ROOT)
    if not 1 <= args.concurrency <= 8:
        raise ValueError("invalid_sandbox_concurrency")
    sandbox = DockerSandbox(ROOT, args.image, timeout=args.render_timeout)
    service = server(
        RenderLauncher(jobs, snapshots, sandbox),
        key_path.read_text().strip(),
        port=args.port,
        concurrency=args.concurrency,
    )
    try:
        service.serve_forever()
    finally:
        service.server_close()


if __name__ == "__main__":
    main()
