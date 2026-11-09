"""Offline draft validation only. No installation, publication, or script execution."""

from __future__ import annotations

import json

from hermes_runtime.skill_contracts import validate_composition
from hermes_runtime.skills import NAME, checked_path, digest, frontmatter

SECTIONS = [
    "适用条件",
    "必要参数",
    "输入输出",
    "前置条件",
    "允许操作",
    "执行流程",
    "完成条件",
    "失败处理",
    "示例",
    "评测",
]


def validate_draft(root, folder, release):
    folder = checked_path(root, folder.relative_to(root).as_posix())
    if not folder.is_relative_to(root / "authoring/drafts"):
        raise ValueError("draft_outside_authoring_area")
    files = {p.name for p in folder.iterdir()}
    if files != {"draft.json", "SKILL.md"}:
        raise ValueError("unsupported_draft_resource")
    data = {}
    for name in files:
        p = checked_path(root, (folder / name).relative_to(root).as_posix())
        if p.stat().st_size > 65536:
            raise ValueError("draft_too_large")
        data[name] = p.read_bytes()
    meta = json.loads(data["draft.json"])
    if set(meta) != {"name", "version", "allowed_operations", "requires_skills", "contract"}:
        raise ValueError("invalid_draft_metadata")
    if (
        not isinstance(meta["name"], str)
        or not NAME.fullmatch(meta["name"])
        or not isinstance(meta["version"], str)
        or not 1 <= len(meta["version"]) <= 64
    ):
        raise ValueError("invalid_draft_identity")
    if meta["name"] in release.entries:
        raise ValueError("draft_identity_must_differ_from_published_skill")
    body = data["SKILL.md"].decode("utf-8")
    frontmatter(body, meta["name"])
    if not all("## " + section in body for section in SECTIONS):
        raise ValueError("draft_template_sections_missing")
    allowed = {op for entry in release.entries.values() for op in entry["allowed_operations"]}
    if (
        not isinstance(meta["allowed_operations"], list)
        or set(meta["allowed_operations"]) - allowed
    ):
        raise ValueError("draft_uses_unregistered_operations")
    validate_composition([*release.entries.values(), meta])
    return {
        "status": "valid_offline_candidate",
        "published": False,
        "executed": False,
        "bundle_sha256": release.bundle_sha256,
        "files": {name: digest(value) for name, value in data.items()},
    }
