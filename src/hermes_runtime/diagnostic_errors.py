"""Bounded failure explanations without exception messages or input values."""

import math
import re
import traceback
from pathlib import Path
from uuid import uuid4

CATALOG = {
    "model_api_error": ("模型请求失败", "按 HTTP 状态、异常类型及重试信息排查模型服务。", False),
    "tool_error": ("工具执行失败", "查看对应的平台工具返回节点和错误编号。", False),
    "invalid_request": ("参数校验未通过", "请按字段说明修正参数，再提交。", False),
    "not_found": ("未找到匹配数据", "核对批次、航班标识和筛选窗口，或扩大查询范围。", False),
    "forbidden": ("当前身份没有权限", "使用有权访问该任务的账号，不要重复提交相同请求。", False),
    "tool_not_allowed": ("当前角色未开放此工具", "核对工具名称、角色和已加载技能。", False),
    "unavailable": ("依赖服务尚不可用", "核对服务健康、运行配置与技能登记。", True),
    "timeout": ("执行超过时间限制", "缩小时间窗口或数据量；确认服务恢复后重试。", True),
    "output_too_large": ("结果超过输出预算", "减小 limit、缩小字段范围或使用分页。", False),
    "internal_error": ("服务内部执行失败", "按错误编号查询服务日志；确认原因后再重试。", False),
}


def validation_details(errors):
    details = []
    for item in errors[:20]:
        if not isinstance(item, dict):
            continue
        code = item.get("type", "invalid")
        code = (
            code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,80}", code) else "invalid"
        )
        location = item.get("loc", [])
        location = location if isinstance(location, (list, tuple)) else []
        field = [
            part
            for part in location[:8]
            if type(part) is int
            or isinstance(part, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", part)
        ]
        context = item.get("ctx")
        context = context if isinstance(context, dict) else {}
        constraints = {
            key: value
            for key, value in context.items()
            if key in {"ge", "gt", "le", "lt", "min_length", "max_length", "multiple_of"}
            and type(value) in (int, float)
            and math.isfinite(value)
        }
        message = {
            "missing": "缺少必填参数",
            "extra_forbidden": "不接受此参数",
            "int_parsing": "必须是整数",
            "int_type": "必须是整数",
            "string_type": "必须是文本",
            "bool_parsing": "必须是布尔值",
        }.get(code, "参数格式或类型不符合要求")
        for key, label in {
            "ge": "必须大于或等于",
            "gt": "必须大于",
            "le": "必须小于或等于",
            "lt": "必须小于",
            "min_length": "长度至少为",
            "max_length": "长度最多为",
            "multiple_of": "必须为该数的整数倍：",
        }.items():
            if key in constraints:
                message = label + str(constraints[key])
                break
        details.append(
            {"field": field, "code": code, "message": message, "constraints": constraints}
        )
    return details


def exception_details(exc, root):
    frames = []
    for frame in traceback.extract_tb(exc.__traceback__)[-5:]:
        path = Path(frame.filename).resolve()
        name = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.name
        frames.append({"file": name, "line": frame.lineno, "function": frame.name})
    return {"error_id": uuid4().hex, "exception_type": type(exc).__name__, "frames": frames}


def describe_failure(value):
    status = value.get("status")
    if not isinstance(status, str) or status not in CATALOG:
        return None
    title, suggestion, retryable = CATALOG[status]
    detail = value.get("diagnostic_error") or value.get("error") or {}
    detail = detail if isinstance(detail, dict) else {}
    result = {"code": status, "title": title, "suggestion": suggestion, "retryable": retryable}
    http_status = detail.get("status_code")
    if type(http_status) is int and 400 <= http_status <= 599:
        result["http_status"] = http_status
        result["message"] = {
            400: "模型服务拒绝请求参数。",
            401: "模型服务凭据校验失败。",
            403: "模型服务拒绝访问。",
            404: "模型名称或接口地址不可用。",
            408: "模型服务处理超时。",
            429: "模型服务限流或账户额度不足。",
        }.get(http_status, "模型服务返回错误，请结合服务端日志核对。")
    for key, pattern in {
        "error_id": r"[a-f0-9]{32}",
        "exception_type": r"[A-Za-z_][A-Za-z0-9_]{0,79}",
    }.items():
        if isinstance(detail.get(key), str) and re.fullmatch(pattern, detail[key]):
            result[key] = detail[key]
    validation = detail.get("validation")
    if isinstance(validation, list):
        # Rebuild explanations from codes and numeric constraints, never trust supplied messages.
        result["validation"] = validation_details(
            [
                {
                    "loc": item.get("field", []),
                    "type": item.get("code", "invalid"),
                    "ctx": item.get("constraints", {}),
                }
                for item in validation[:20]
                if isinstance(item, dict)
            ]
        )
    fields = value.get("fields", value.get("validation_fields", []))
    if not result.get("validation") and isinstance(fields, list):
        result["validation"] = validation_details(
            [{"loc": field} for field in fields[:20] if isinstance(field, list)]
        )
    frames = detail.get("frames", [])
    if isinstance(frames, list):
        result["frames"] = [
            {"file": item["file"], "line": item["line"], "function": item["function"]}
            for item in frames[:5]
            if isinstance(item, dict)
            and isinstance(item.get("file"), str)
            and re.fullmatch(r"[A-Za-z0-9_./-]{1,160}", item["file"])
            and not item["file"].startswith("/")
            and ".." not in item["file"].split("/")
            and type(item.get("line")) is int
            and isinstance(item.get("function"), str)
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,100}", item["function"])
        ]
    return result
