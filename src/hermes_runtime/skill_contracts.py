"""Platform release contracts for skill composition; no workflow executor."""

from __future__ import annotations

import re


def dependency_depth(entries):
    """Root is depth one; validated release dependencies contain no cycles."""

    def depth(name, seen):
        if name in seen:
            raise ValueError("skill_dependency_cycle")
        children = entries[name].get("requires_skills", {})
        return 1 + max((depth(child, seen | {name}) for child in children), default=0)

    return max((depth(name, set()) for name in entries), default=0)


def validate_composition(entries):
    """Validate exact-version dependencies and named input/output contracts.

    Dependencies describe compatible published resources, not inherited privileges.
    Actual child bodies must still be loaded for each business operation.
    """
    names = {entry["name"]: entry for entry in entries}
    pattern = re.compile(r"[a-z][a-z0-9.-]{0,99}\Z")
    for entry in entries:
        contract = entry.get("contract")
        if (
            not isinstance(contract, dict)
            or set(contract) != {"version", "inputs", "outputs"}
            or contract["version"] != 1
        ):
            raise ValueError("invalid_skill_contract")
        for field in ["inputs", "outputs"]:
            values = contract[field]
            if (
                not isinstance(values, list)
                or not values
                or len(values) > 16
                or not all(isinstance(x, str) and pattern.fullmatch(x) for x in values)
            ):
                raise ValueError("invalid_skill_contract")
        dependencies = entry.get("requires_skills")
        if not isinstance(dependencies, dict) or len(dependencies) > 16:
            raise ValueError("invalid_skill_dependencies")
        for name, version in dependencies.items():
            if name not in names or version != names[name]["version"]:
                raise ValueError("skill_dependency_version_mismatch")
    visiting, done = set(), set()

    def visit(name):
        if name in visiting:
            raise ValueError("skill_dependency_cycle")
        if name in done:
            return
        visiting.add(name)
        for dependency in names[name]["requires_skills"]:
            visit(dependency)
        visiting.remove(name)
        done.add(name)

    for name in names:
        visit(name)
