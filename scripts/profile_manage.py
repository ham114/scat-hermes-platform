"""Unified SCAT native profile manager; never prints credentials or raw profile files."""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "vendor/hermes-agent")]


def main():
    if sys.version_info[:2] != (3, 14) or not (Path(sys.prefix) / "conda-meta").is_dir():
        raise RuntimeError("Use existing scat-hermes Anaconda Python")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "list", "show", "doctor", "set"])
    parser.add_argument("--profile", choices=["scat-main", "scat-worker", "scat-author"])
    parser.add_argument("--root", help="Project-relative managed root")
    parser.add_argument("--reuse-root-key", action="store_true")
    parser.add_argument("--expected-sha256")
    parser.add_argument("--model")
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--max-turns", type=int)
    args = parser.parse_args()
    from hermes_runtime.native_profiles import initialize, update_settings, verify_native
    from hermes_runtime.profiles import ROLES, metadata, profile_root

    temporary = ROOT / ".tmp/profiles"
    temporary.mkdir(parents=True, exist_ok=True)
    os.environ.update(
        HERMES_DISABLE_LAZY_INSTALLS="1",
        HERMES_HOME=str(profile_root(ROOT, args.root)),
        TMP=str(temporary),
        TEMP=str(temporary),
        TMPDIR=str(temporary),
    )
    try:
        if args.action == "init":
            result = initialize(ROOT, args.root, reuse_key=args.reuse_root_key)
        elif args.action == "set":
            if not args.profile or not args.expected_sha256:
                parser.error("set requires --profile and --expected-sha256")
            changes = {
                key: value
                for key, value in {
                    "model": args.model,
                    "temperature": args.temperature,
                    "max_turns": args.max_turns,
                }.items()
                if value is not None
            }
            result = update_settings(ROOT, args.profile, changes, args.expected_sha256, args.root)
        else:
            verify_native(ROOT)
            names = [args.profile] if args.profile else list(ROLES)
            result = {"profiles": [metadata(ROOT, name, base=args.root) for name in names]}
            if args.action == "doctor":
                result["status"] = (
                    "ok"
                    if all(
                        p["directories_valid"] and p["state_db_exists"] for p in result["profiles"]
                    )
                    else "failed"
                )
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, OSError):
        print(json.dumps({"status": "failed", "error": "profile_operation_rejected"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
