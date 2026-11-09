"""Actual isolated child cases; author examples supplement trusted native holdouts."""

import json
from uuid import uuid4

from hermes_runtime.script_language import ScriptError, encode
from hermes_runtime.script_policy import load_script_policy
from hermes_runtime.script_resources import descriptors
from hermes_runtime.script_runtime import ScriptRunner, ScriptStore, sha
from hermes_runtime.skills import digest


def run_script_cases(root, release, trial_id):
    resources = descriptors(release.entries, release.files)
    directory = root / "var/validation/20261028-s7b"
    directory.mkdir(parents=True, exist_ok=True)
    store = ScriptStore(directory / ("cases-" + trial_id + ".sqlite"), root)
    runner = ScriptRunner(root, store, load_script_policy(root))
    outcomes = []
    for (name, path), descriptor in resources.items():
        for index, case in enumerate(descriptor["cases"]):
            calls = []

            def dispatch(operation, arguments, identity, seq, *, case=case, calls=calls):
                expected = case["calls"][len(calls)]
                if operation != expected["operation"] or sha(arguments) != sha(
                    expected["arguments"]
                ):
                    raise ScriptError("script_fixture_call_mismatch")
                calls.append(operation)
                return expected["result"], None

            item = {
                "skill": name,
                "script": path,
                "case": index,
                "script_sha256": digest(release.files[name + "/" + path]),
                "passed": False,
            }
            try:
                result = runner.run(
                    scope="script-trial:" + trial_id,
                    run=name + ":" + path + ":" + str(index),
                    request_id=str(uuid4()),
                    source=release.files[name + "/" + path].decode(),
                    descriptor=descriptor,
                    inputs=case["inputs"],
                    metadata={"bundle_sha256": release.bundle_sha256},
                    dispatch=dispatch,
                )
                item["passed"] = encode(result["output"]) == encode(case["expected"]) and len(
                    calls
                ) == len(case["calls"])
                item["calls"] = len(calls)
            except (ValueError, TypeError, KeyError, IndexError, OSError):
                item["error"] = "isolated_script_case_failed"
            outcomes.append(item)
    report = {
        "status": "passed" if all(item["passed"] for item in outcomes) else "failed",
        "runtime": "scat-thin-python-json-v1",
        "bundle_sha256": release.bundle_sha256,
        "scripts": [
            {"skill": name, "path": path, "sha256": digest(release.files[name + "/" + path])}
            for name, path in resources
        ],
        "cases": outcomes,
        "synthetic_only": True,
    }
    (directory / ("cases-" + trial_id + ".json")).write_text(json.dumps(report, indent=2))
    return report
