"""Inspect policy/schema, render investigation parameters, or validate an offline draft."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main():
    from hermes_runtime.skill_authoring import validate_draft
    from hermes_runtime.skill_policy import SkillPolicy, load_policy
    from hermes_runtime.skill_templates import load_investigation
    from hermes_runtime.skills import SkillRelease, checked_path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["policy", "schema", "render", "check-draft"])
    parser.add_argument("--path")
    parser.add_argument("--budget", default="{}")
    args = parser.parse_args()
    if args.action == "schema":
        result = SkillPolicy.model_json_schema()
    elif args.action == "policy":
        result = load_policy(ROOT).describe(request=json.loads(args.budget))
    else:
        if not args.path:
            parser.error("--path is required")
        path = checked_path(ROOT, args.path)
        release = SkillRelease(ROOT, ROOT / "services/hermes/skills.lock.json")
        result = (
            load_investigation(path).prepare(release, load_policy(ROOT))
            if args.action == "render"
            else validate_draft(ROOT, path, release)
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
