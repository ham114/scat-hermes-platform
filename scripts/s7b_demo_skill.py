"""Synthetic review candidate, never installed into the active business bundle."""

import json

SCRIPT = """def run(inputs):
    found = call("mcp__scat__search_scat_flights", {"batch": inputs["batch"], "callsign": inputs["callsign"], "limit": inputs["search_limit"]})
    if get(found, "status") != "ok":
        return {"status": "error", "flights": 0, "points": 0, "altitude_m": 0, "missing_points": 0, "external_ids": [], "synthetic": get(found, "synthetic", False)}
    items = found["data"]["items"]
    ids = []
    for item in items:
        ids = append(ids, item["source"]["external_id"])
    if len(ids) == 0:
        return {"status": "empty", "flights": 0, "points": 0, "altitude_m": 0, "missing_points": 0, "external_ids": [], "synthetic": get(found, "synthetic", False)}
    quality = call("mcp__scat__inspect_scat_collection_data_quality", {"batch": inputs["batch"]})
    track = call("mcp__scat__get_scat_track_points", {"batch": inputs["batch"], "external_id": ids[0], "limit": inputs["track_limit"]})
    if get(quality, "status") != "ok" or get(track, "status") != "ok":
        return {"status": "error", "flights": len(ids), "points": 0, "altitude_m": 0, "missing_points": 0, "external_ids": ids, "synthetic": get(found, "synthetic", False)}
    points = track["data"]["points"]
    altitude = points[0]["position"]["altitude_m"] if len(points) > 0 else 0
    return {"status": "ok", "flights": len(ids), "points": len(points), "altitude_m": altitude, "missing_points": quality["data"]["missing_point_count"], "external_ids": ids, "synthetic": get(found, "synthetic", False)}
"""


def content(release):
    string = {"type": "string", "minLength": 1, "maxLength": 100}
    integer = {"type": "integer", "minimum": 0, "maximum": 10000}
    inputs = {"batch": "SYNTH-S6-HOLDOUT", "callsign": "QA601", "search_limit": 1, "track_limit": 2}
    identifier = "SYNTH-S6-HOLDOUT-001"
    found = {
        "status": "ok",
        "synthetic": True,
        "data": {"items": [{"source": {"external_id": identifier}}]},
    }
    expected = {
        "status": "ok",
        "flights": 1,
        "points": 2,
        "altitude_m": 1000,
        "missing_points": 0,
        "external_ids": [identifier],
        "synthetic": True,
    }
    operations = [
        "mcp__scat__search_scat_flights",
        "mcp__scat__inspect_scat_collection_data_quality",
        "mcp__scat__get_scat_track_points",
    ]
    desc = {
        "version": 1,
        "runtime": "scat-thin-python-json-v1",
        "operations": operations,
        "input_schema": {
            "type": "object",
            "properties": {
                "batch": string,
                "callsign": string,
                "search_limit": {"type": "integer", "minimum": 1, "maximum": 10},
                "track_limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": list(inputs),
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "maxLength": 10, "enum": ["ok", "empty", "error"]},
                "flights": integer,
                "points": integer,
                "altitude_m": {"type": "number", "minimum": -1000, "maximum": 1000000},
                "missing_points": integer,
                "external_ids": {"type": "array", "maxItems": 10, "items": string},
                "synthetic": {"type": "boolean"},
            },
            "required": list(expected),
            "additionalProperties": False,
        },
        "cases": [],
    }
    calls = [
        {
            "operation": operations[0],
            "arguments": {"batch": inputs["batch"], "callsign": "QA601", "limit": 1},
            "result": found,
        },
        {
            "operation": operations[1],
            "arguments": {"batch": inputs["batch"]},
            "result": {"status": "ok", "data": {"missing_point_count": 0}},
        },
        {
            "operation": operations[2],
            "arguments": {"batch": inputs["batch"], "external_id": identifier, "limit": 2},
            "result": {
                "status": "ok",
                "data": {
                    "points": [
                        {"position": {"altitude_m": 1000}},
                        {"position": {"altitude_m": 1010}},
                    ]
                },
            },
        },
    ]
    desc["cases"].append({"inputs": inputs, "calls": calls, "expected": expected})
    for status, result in [
        ("empty", {"status": "ok", "synthetic": True, "data": {"items": []}}),
        ("error", {"status": "error", "synthetic": True, "data": {}}),
    ]:
        desc["cases"].append(
            {
                "inputs": inputs,
                "calls": [calls[0] | {"result": result}],
                "expected": expected
                | {
                    "status": status,
                    "flights": 0,
                    "points": 0,
                    "altitude_m": 0,
                    "external_ids": [],
                },
            }
        )
    name = "s7b-flight-investigation"
    sections = {
        "适用条件": "有明确批次/呼号的只读调查；不执行预测或数据库写入。",
        "必要参数": "batch、callsign、search_limit、track_limit必须由用户目标或实际工具结果提供。读取scripts/investigate.json获取类型和范围；不猜标识。",
        "输入输出": "输入scat.request.v1；脚本输出航班/点数、高度米、缺失点及真实external_ids。空/错误明确标注，不把默认0当有效统计。",
        "前置条件": "先加载本技能，再分别加载scat-flight-query、scat-data-quality、scat-trajectory-analysis；依赖不继承权限。",
        "允许操作": "通过已加载子技能使用三个登记的只读操作，脚本本身没有额外权限。",
        "执行流程": "读取脚本描述后调用skill_script_run(skill=s7b-flight-investigation,script=scripts/investigate.py,inputs=实际参数,request_id=稳定UUID)。脚本查询→有界整理标识→空结果停止→质量→轨迹→整理；依据真实回执答复。",
        "完成条件": "实际执行脚本并核对business_result_references，状态ok且出处、范围、单位一致；合成数据必须标明合成。",
        "失败处理": "缺参先补问。empty/error/unknown均不得宣称完成；unknown不换UUID重放；权限拒绝时检查实际技能加载。",
        "示例": "合成留出条件由测试平台给出。查询1个航班、读2点时传search_limit=1、track_limit=2，不把示例标识用于真实业务。",
        "评测": "三项脚本合成用例（成功、空、错误），及独立原生留出三次；不能用作者用例替代可信原生证据。",
    }
    body = (
        "---\nname: "
        + name
        + "\ndescription: 受限脚本演示航班查询、质量与轨迹只读编排，实际工具引用可回查。\n---\n\n"
    )
    body += "\n".join("## " + title + "\n\n" + text + "\n" for title, text in sections.items())
    return {
        "changes": [
            {
                "name": name,
                "version": "1.0.0",
                "allowed_operations": [],
                "requires_skills": {
                    n: release.entries[n]["version"]
                    for n in ["scat-flight-query", "scat-data-quality", "scat-trajectory-analysis"]
                },
                "contract": {
                    "version": 1,
                    "inputs": ["scat.request.v1"],
                    "outputs": ["scat.query-result.v1"],
                },
                "files": {
                    "SKILL.md": body,
                    "scripts/investigate.py": SCRIPT,
                    "scripts/investigate.json": json.dumps(desc, ensure_ascii=False),
                },
            }
        ]
    }
