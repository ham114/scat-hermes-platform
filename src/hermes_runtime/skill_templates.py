"""Validated reusable readonly investigations; templates grant no capabilities."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import Field

from hermes_runtime.skill_policy import Budget, StrictModel


class Investigation(StrictModel):
    schema_version: Literal[1] = 1
    template_id: Literal["scat-investigation-v1"] = "scat-investigation-v1"
    skill_version: str = "0.2.0"
    batch: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")]
    callsign: Annotated[str, Field(pattern=r"^[A-Za-z0-9-]{1,20}$")]
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 1
    track_limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 3
    quality: bool = True
    trajectory: bool = True
    knowledge: bool = True
    budget: dict[str, int] = Field(default_factory=dict)

    def prepare(self, release, policy):
        parent = release.entries.get("scat-investigation")
        if parent is None or parent["version"] != self.skill_version:
            raise ValueError("template_skill_version_mismatch")
        selected = ["scat-investigation", "scat-flight-query"]
        for enabled, name in [
            (self.quality, "scat-data-quality"),
            (self.trajectory, "scat-trajectory-analysis"),
            (self.knowledge, "aviation-knowledge"),
        ]:
            if enabled:
                selected.append(name)
        for name in selected[1:]:
            if parent["requires_skills"].get(name) != release.entries[name]["version"]:
                raise ValueError("template_dependency_mismatch")
        limits: Budget = policy.effective(category="investigation", request=self.budget)
        tasks = [f"实际查询批次 {self.batch} 中呼号 {self.callsign}，最多 {self.limit} 个航班"]
        if self.quality:
            tasks.append("检查同一批次质量")
        if self.trajectory:
            tasks.append(f"用查询实际返回的 batch/external_id 读取最多 {self.track_limit} 个轨迹点")
        if self.knowledge:
            tasks.append("检索缺失率术语，并引用实际 source_id")
        prompt = (
            "请先 skills_list，再显式加载 "
            + "、".join(selected)
            + " 的正文；按查询技能要求读取 references/query-contract.md。"
            + "；".join(tasks)
            + "。参数缺失需补问，空结果停止依赖该航班的步骤，失败如实说明；"
            "只执行选定分支，不执行模型任务。汇总实际结果、范围、出处和限制。"
        )
        return {
            "template": self.model_dump(),
            "selected_skills": selected,
            "bundle_sha256": release.bundle_sha256,
            "policy_sha256": policy.sha256,
            "effective_budget": limits.model_dump(),
            "prompt": prompt,
        }


def load_investigation(path):
    if path.stat().st_size > 65536:
        raise ValueError("template_too_large")
    return Investigation.model_validate(json.loads(path.read_text(encoding="utf-8")))
