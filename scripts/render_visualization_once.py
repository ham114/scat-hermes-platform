"""Operational isolated renderer smoke entry; reads source/data on stdin, never API keys."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    from visualization_runtime.paths import confined_path
    from visualization_runtime.sandbox import DockerSandbox, RenderError

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--cleanup-only", action="store_true")
    args = parser.parse_args()
    renderer = DockerSandbox(ROOT, args.image, timeout=args.timeout)
    try:
        if args.cleanup_only:
            renderer.remove(args.name)
            result = {"stopped": renderer.stopped(args.name)}
        else:
            raw = sys.stdin.buffer.read(21 * 1024 * 1024 + 1)
            if len(raw) > 21 * 1024 * 1024:
                raise RenderError("render_input_too_large")
            result = renderer.render(
                args.name, confined_path(ROOT, Path(args.directory)), json.loads(raw)
            )
        print(json.dumps(result))
        return 0
    except RenderError as error:
        result = {"state": "failed", "error": str(error)}
        if error.diagnostic:
            result["diagnostic"] = error.diagnostic
        print(json.dumps(result))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
