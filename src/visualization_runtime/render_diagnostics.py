"""Bounded repair evidence; never forward raw browser messages/stacks or stderr."""

import re


def diagnostic(value, *, source_lines=200000):
    if not isinstance(value, dict) or set(value) - {
        "stage",
        "viewport",
        "error_type",
        "message_code",
        "source_line",
        "source_column",
        "property",
        "receiver",
    }:
        return None
    if (
        value.get("stage")
        not in (
            "module_load",
            "render",
            "browser_launch",
            "browser_setup",
            "generated_code",
            "browser_execution",
            "screenshot",
        )
        or value.get("error_type")
        not in (
            "Error",
            "TypeError",
            "ReferenceError",
            "SyntaxError",
            "RangeError",
            "URIError",
            "EvalError",
        )
        or value.get("message_code")
        not in (
            "execution_error",
            "missing_property",
            "undefined_identifier",
        )
    ):
        return None
    if "viewport" in value and value["viewport"] not in ("desktop", "mobile"):
        return None
    for key, maximum in (("source_line", min(source_lines, 200000)), ("source_column", 100000)):
        if key in value and (type(value[key]) is not int or not 1 <= value[key] <= maximum):
            return None
    prop = value.get("property")
    if prop is not None and (
        not isinstance(prop, str) or not re.fullmatch(r"[A-Za-z_$][\w$]{0,63}", prop, re.ASCII)
    ):
        return None
    code = value["message_code"]
    if code == "missing_property":
        if (
            value["error_type"] != "TypeError"
            or not prop
            or value.get("receiver") not in ("undefined", "null")
        ):
            return None
    elif code == "undefined_identifier":
        if value["error_type"] != "ReferenceError" or not prop or "receiver" in value:
            return None
    elif "property" in value or "receiver" in value:
        return None
    return dict(value)


def failure_message(code):
    return {
        "render_not_passed": "绘图脚本在渲染阶段执行失败，自动修订次数已用尽，未进入本候选的视觉审核。",
        "review_not_passed": "图表已完成渲染，但未通过独立视觉审核。",
        "render_execution_failed": "绘图脚本执行失败，未生成可供审核的图像。",
        "render_timeout": "绘图脚本渲染超时，未完成图像生成。",
    }.get(code)
