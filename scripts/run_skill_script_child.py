"""Trusted isolated child for a JSON-only AST interpreter, not Python exec."""

import json
import os
import resource
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from hermes_runtime.script_language import Interpreter, ScriptError, encode, validate_value


def frame(value):
    sys.stdout.write(encode(value, 524288) + "\n")
    sys.stdout.flush()


def main():
    if os.name != "posix" or not (Path(sys.prefix) / "conda-meta").is_dir():
        raise ScriptError("script_existing_conda_required")
    line = sys.stdin.buffer.readline(524289)
    if len(line) > 524288:
        raise ScriptError("script_frame_too_large")
    initial = json.loads(line)
    limits = initial["limits"]
    resource.setrlimit(resource.RLIMIT_CPU, (limits["cpu_seconds"], limits["cpu_seconds"]))
    resource.setrlimit(resource.RLIMIT_AS, (limits["memory_mb"] * 1024**2,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
    resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    count = 0

    def call(operation, arguments):
        nonlocal count
        count += 1
        if count > limits["max_tool_calls"]:
            raise ScriptError("script_tool_budget_exhausted")
        frame({"type": "call", "seq": count, "operation": operation, "arguments": arguments})
        raw = sys.stdin.buffer.readline(limits["max_frame_bytes"] + 1)
        if not raw or len(raw) > limits["max_frame_bytes"]:
            raise ScriptError("script_parent_unavailable")
        response = json.loads(raw)
        if response.get("seq") != count or set(response) != {"seq", "result"}:
            raise ScriptError("script_parent_protocol_invalid")
        return response["result"]

    descriptor = initial["descriptor"]
    validate_value(initial["inputs"], descriptor["input_schema"])
    output = Interpreter(
        initial["source"],
        descriptor["operations"],
        initial["inputs"],
        call,
        max_steps=limits["max_steps"],
        max_iterations=limits["max_iterations"],
        max_value_bytes=limits["max_frame_bytes"],
    ).run()
    validate_value(output, descriptor["output_schema"])
    encode(output, limits["max_output_bytes"])
    frame({"type": "result", "output": output})


if __name__ == "__main__":
    try:
        main()
    except (ScriptError, ValueError, TypeError, KeyError, MemoryError, RecursionError) as exc:
        frame(
            {
                "type": "error",
                "error": str(exc) if isinstance(exc, ScriptError) else "script_child_failed",
            }
        )
        raise SystemExit(1) from None
