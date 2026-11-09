"""Signed paired script resources and bounded, explicit JSON contracts."""

import json
import re

from hermes_runtime.script_language import (
    RUNTIME,
    ScriptError,
    encode,
    parse,
    schema_definition,
    validate_value,
)

SCRIPT_PATH = re.compile(r"scripts/[a-z][a-z0-9_-]{0,63}\.py\Z")


def descriptors(entries, files):
    result = {}
    for name, entry in entries.items():
        paths = {path for path in entry["files"] if path.startswith("scripts/")}
        if not paths:
            continue
        python = {path for path in paths if SCRIPT_PATH.fullmatch(path)}
        if paths != python | {path[:-3] + ".json" for path in python}:
            raise ScriptError("script_paired_resources_required")
        if len(python) > 4:
            raise ScriptError("script_resource_capacity")
        closure, pending = set(), [name]
        while pending:
            current = pending.pop()
            if current not in closure:
                closure.add(current)
                pending.extend(entries[current]["requires_skills"])
        allowed = {op for child in closure for op in entries[child]["allowed_operations"]}
        for path in sorted(python):
            source = files[name + "/" + path].decode("utf-8")
            try:
                item = json.loads(files[name + "/" + path[:-3] + ".json"])
            except (ValueError, UnicodeError):
                raise ScriptError("script_descriptor_invalid") from None
            if (
                not isinstance(item, dict)
                or set(item)
                != {"version", "runtime", "operations", "input_schema", "output_schema", "cases"}
                or type(item["version"]) is not int
                or item["version"] != 1
                or item["runtime"] != RUNTIME
            ):
                raise ScriptError("script_descriptor_invalid")
            ops = item["operations"]
            if (
                not isinstance(ops, list)
                or len(ops) > 16
                or any(type(op) is not str for op in ops)
                or len(set(ops)) != len(ops)
                or set(ops) - allowed
            ):
                raise ScriptError("script_operation_rejected")
            _, used = parse(source, set(ops))
            if used != set(ops):
                raise ScriptError("script_operation_declaration_mismatch")
            for schema in (item["input_schema"], item["output_schema"]):
                schema_definition(schema)
            if item["input_schema"]["type"] != "object":
                raise ScriptError("script_object_input_required")
            cases = item["cases"]
            if not isinstance(cases, list) or not 1 <= len(cases) <= 8:
                raise ScriptError("script_cases_required")
            for case in cases:
                if (
                    not isinstance(case, dict)
                    or set(case) != {"inputs", "calls", "expected"}
                    or not isinstance(case["calls"], list)
                    or len(case["calls"]) > 12
                ):
                    raise ScriptError("script_case_invalid")
                validate_value(case["inputs"], item["input_schema"])
                validate_value(case["expected"], item["output_schema"])
                for call in case["calls"]:
                    if (
                        not isinstance(call, dict)
                        or set(call) != {"operation", "arguments", "result"}
                        or call["operation"] not in ops
                        or not isinstance(call["arguments"], dict)
                    ):
                        raise ScriptError("script_case_invalid")
                encode(case, 65536)
            result[(name, path)] = item
    return result
