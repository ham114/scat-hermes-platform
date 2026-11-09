"""M6-07 实验产物存储：评估报告 / 训练日志 / 模型文件的文件级留存。

- 目录根：`settings.SCAT_EXPERIMENT_ARTIFACT_DIR`（默认 `var/scat-experiment-artifacts`）；
- 命名：`<experiment_id>/<name>`（路径组件白名单过滤，防目录穿越）；
- 写入后回传元数据 `{name, kind, path, sha256, size, content_type, created_at}`，
  由实验记录 `artifacts` 字段留痕——相对路径 + 校验和，可校验、可迁移；
- 只写产物目录：不写原始集合与派生集合；单产物上限 20 MiB
  （训练日志与模型文件过大时按分片/外部存储扩展，本层只做登记）。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core import settings

MAX_ARTIFACT_BYTES = 20 * 1024 * 1024

__all__ = ["MAX_ARTIFACT_BYTES", "ExperimentArtifactStore"]


class ExperimentArtifactStore:
    """把实验产物写入文件系统并返回可校验的元数据。"""

    def __init__(self, root: Path | str | None = None) -> None:
        self._root = Path(root) if root is not None else Path(settings.SCAT_EXPERIMENT_ARTIFACT_DIR)

    @property
    def root(self) -> Path:
        return self._root

    def store(
        self,
        *,
        experiment_id: str,
        name: str,
        payload: bytes | str | dict[str, Any],
        kind: str = "report",
    ) -> dict[str, Any]:
        """写入一个产物；返回含 sha256/size 的元数据字典。"""
        safe_experiment = _safe_component(experiment_id)
        safe_name = _safe_component(name)
        if isinstance(payload, dict):
            data = json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
            content_type = "application/json"
        elif isinstance(payload, str):
            data = payload.encode("utf-8")
            content_type = "text/plain; charset=utf-8"
        else:
            data = bytes(payload)
            content_type = "application/octet-stream"
        if len(data) > MAX_ARTIFACT_BYTES:
            raise ValueError(
                f"产物 {safe_name} 超过上限 {MAX_ARTIFACT_BYTES} 字节（{len(data)} 字节）"
            )
        directory = self._root / safe_experiment
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / safe_name
        path.write_bytes(data)
        return {
            "name": safe_name,
            "kind": kind,
            "path": f"{safe_experiment}/{safe_name}",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "content_type": content_type,
            "created_at": datetime.now(UTC).isoformat(),
        }

    def resolve(self, *, experiment_id: str, name: str) -> Path:
        """解析产物绝对路径（校验路径组件；不检查文件是否存在）。"""
        return self._root / _safe_component(experiment_id) / _safe_component(name)


def _safe_component(value: str) -> str:
    cleaned = "".join(
        character for character in str(value) if character.isalnum() or character in "._-"
    )
    if not cleaned or cleaned in {".", ".."} or set(cleaned) <= {"."}:
        raise ValueError(f"非法的产物路径组件：{value!r}")
    return cleaned[:140]
