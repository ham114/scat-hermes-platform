"""A bounded JSON-only Python syntax interpreter. Never eval/exec user code."""

from __future__ import annotations

import ast
import json
import math
import re

RUNTIME = "scat-thin-python-json-v1"
HELPERS = frozenset({"call", "get", "len", "append", "range"})
IDENTIFIER = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,63}\Z")


class ScriptError(ValueError):
    pass


def encode(value, limit=262144):
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise ScriptError("script_json_required") from None
    if len(raw.encode()) > limit:
        raise ScriptError("script_value_too_large")
    return raw


def schema_definition(schema, depth=0):
    if depth > 8 or not isinstance(schema, dict):
        raise ScriptError("script_schema_invalid")
    kind = schema.get("type")
    if not isinstance(kind, str):
        raise ScriptError("script_schema_invalid")
    fields = {"type", "enum", "description"}
    if kind == "object":
        fields |= {"properties", "required", "additionalProperties"}
        properties, required = schema.get("properties"), schema.get("required", [])
        if (
            not isinstance(properties, dict)
            or len(properties) > 32
            or schema.get("additionalProperties") is not False
            or not isinstance(required, list)
            or any(not isinstance(key, str) or key not in properties for key in required)
        ):
            raise ScriptError("script_schema_invalid")
        for key, value in properties.items():
            if not isinstance(key, str) or len(key) > 100:
                raise ScriptError("script_schema_invalid")
            schema_definition(value, depth + 1)
    elif kind == "array":
        fields |= {"items", "maxItems"}
        maximum = schema.get("maxItems")
        if type(maximum) is not int or not 0 <= maximum <= 256:
            raise ScriptError("script_schema_invalid")
        schema_definition(schema.get("items"), depth + 1)
    elif kind == "string":
        fields |= {"maxLength", "minLength"}
        if type(schema.get("maxLength")) is not int or not 1 <= schema["maxLength"] <= 32768:
            raise ScriptError("script_schema_invalid")
        if (
            type(schema.get("minLength", 0)) is not int
            or not 0 <= schema.get("minLength", 0) <= schema["maxLength"]
        ):
            raise ScriptError("script_schema_invalid")
    elif kind in {"number", "integer"}:
        fields |= {"minimum", "maximum"}
        if (
            any(
                type(schema.get(x)) not in (int, float)
                or abs(schema[x]) > 10**12
                or not math.isfinite(schema[x])
                for x in ("minimum", "maximum")
            )
            or schema["minimum"] > schema["maximum"]
        ):
            raise ScriptError("script_schema_invalid")
    elif kind not in {"boolean", "null"}:
        raise ScriptError("script_schema_invalid")
    if set(schema) - fields:
        raise ScriptError("script_schema_field_rejected")
    if "enum" in schema and (
        not isinstance(schema["enum"], list) or not 1 <= len(schema["enum"]) <= 32
    ):
        raise ScriptError("script_schema_invalid")
    encode(schema, 32768)


def validate_value(value, schema, depth=0):
    if depth > 8:
        raise ScriptError("script_schema_mismatch")
    kind = schema["type"]
    valid = {
        "object": type(value) is dict,
        "array": type(value) is list,
        "string": type(value) is str,
        "integer": type(value) is int,
        "number": type(value) in (int, float),
        "boolean": type(value) is bool,
        "null": value is None,
    }[kind]
    if not valid or "enum" in schema and value not in schema["enum"]:
        raise ScriptError("script_schema_mismatch")
    if kind == "object":
        if (
            set(value) - schema["properties"].keys()
            or set(schema.get("required", [])) - value.keys()
        ):
            raise ScriptError("script_schema_mismatch")
        for key, item in value.items():
            validate_value(item, schema["properties"][key], depth + 1)
    elif kind == "array":
        if len(value) > schema["maxItems"]:
            raise ScriptError("script_schema_mismatch")
        for item in value:
            validate_value(item, schema["items"], depth + 1)
    elif (
        kind == "string"
        and not schema.get("minLength", 0) <= len(value) <= schema["maxLength"]
        or kind in {"number", "integer"}
        and (
            abs(value) > 10**12
            or not math.isfinite(value)
            or not schema["minimum"] <= value <= schema["maximum"]
        )
    ):
        raise ScriptError("script_schema_mismatch")


def parse(source, operations):
    if not isinstance(source, str) or len(source.encode()) > 16384:
        raise ScriptError("script_source_too_large")
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        raise ScriptError("script_syntax_invalid") from None
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise ScriptError("script_single_run_required")
    function = tree.body[0]
    args = function.args
    if (
        function.name != "run"
        or function.decorator_list
        or function.returns
        or getattr(function, "type_params", [])
        or len(args.args) != 1
        or args.args[0].arg != "inputs"
        or args.args[0].annotation
        or args.vararg
        or args.kwarg
        or args.kwonlyargs
        or args.posonlyargs
        or args.defaults
    ):
        raise ScriptError("script_run_signature_invalid")
    supported = (
        ast.Assign,
        ast.Return,
        ast.If,
        ast.For,
        ast.Expr,
        ast.Name,
        ast.Constant,
        ast.Dict,
        ast.List,
        ast.Subscript,
        ast.Compare,
        ast.BoolOp,
        ast.UnaryOp,
        ast.Call,
        ast.IfExp,
        ast.Load,
        ast.Store,
        ast.Eq,
        ast.NotEq,
        ast.In,
        ast.NotIn,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.And,
        ast.Or,
        ast.Not,
        ast.USub,
    )
    used = set()
    nodes = list(ast.walk(ast.Module(body=function.body, type_ignores=[])))
    if len(nodes) > 1000:
        raise ScriptError("script_ast_too_large")
    for node in nodes[1:]:
        if not isinstance(node, supported):
            raise ScriptError("script_syntax_rejected")
        if isinstance(node, ast.Name) and (
            not IDENTIFIER.fullmatch(node.id)
            or isinstance(node.ctx, ast.Store)
            and node.id in HELPERS | {"inputs"}
        ):
            raise ScriptError("script_name_rejected")
        if isinstance(node, ast.Assign) and (
            len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name)
        ):
            raise ScriptError("script_assignment_rejected")
        if isinstance(node, ast.For) and (not isinstance(node.target, ast.Name) or node.orelse):
            raise ScriptError("script_loop_rejected")
        if isinstance(node, ast.Dict) and any(
            not isinstance(key, ast.Constant) or type(key.value) is not str for key in node.keys
        ):
            raise ScriptError("script_dict_keys_required")
        if isinstance(node, ast.Constant):
            if type(node.value) not in (str, int, float, bool, type(None)):
                raise ScriptError("script_constant_rejected")
            if type(node.value) in (int, float) and (
                abs(node.value) > 10**12 or not math.isfinite(node.value)
            ):
                raise ScriptError("script_constant_rejected")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in HELPERS or node.keywords:
                raise ScriptError("script_call_rejected")
            if node.func.id == "call":
                if (
                    len(node.args) != 2
                    or not isinstance(node.args[0], ast.Constant)
                    or node.args[0].value not in operations
                ):
                    raise ScriptError("script_operation_rejected")
                used.add(node.args[0].value)
    return function.body, used


class Interpreter:
    def __init__(
        self,
        source,
        operations,
        inputs,
        dispatch,
        *,
        max_steps=2000,
        max_iterations=64,
        max_value_bytes=262144,
    ):
        self.body, _ = parse(source, operations)
        self.env = {"inputs": inputs}
        self.dispatch = dispatch
        self.remaining, self.iterations = max_steps, max_iterations
        self.max_value_bytes = max_value_bytes

    def expr(self, node):
        self.tick()
        if isinstance(node, ast.Constant):
            value = node.value
        elif isinstance(node, ast.Name):
            if node.id not in self.env:
                raise ScriptError("script_variable_missing")
            value = self.env[node.id]
        elif isinstance(node, ast.Dict):
            value = {
                key.value: self.expr(item) for key, item in zip(node.keys, node.values, strict=True)
            }
        elif isinstance(node, ast.List):
            value = [self.expr(item) for item in node.elts]
        elif isinstance(node, ast.Subscript):
            container, key = self.expr(node.value), self.expr(node.slice)
            if (
                type(container) is dict
                and type(key) is str
                or type(container) is list
                and type(key) is int
            ):
                try:
                    value = container[key]
                except (KeyError, IndexError):
                    raise ScriptError("script_key_missing") from None
            else:
                raise ScriptError("script_subscript_rejected")
        elif isinstance(node, ast.UnaryOp):
            operand = self.expr(node.operand)
            if isinstance(node.op, ast.Not):
                value = not operand
            elif type(operand) in (int, float):
                value = -operand
            else:
                raise ScriptError("script_unary_rejected")
        elif isinstance(node, ast.BoolOp):
            value = self.expr(node.values[0])
            for item in node.values[1:]:
                if (
                    isinstance(node.op, ast.And)
                    and not value
                    or isinstance(node.op, ast.Or)
                    and value
                ):
                    break
                value = self.expr(item)
        elif isinstance(node, ast.Compare):
            left, value = self.expr(node.left), True
            for op, item in zip(node.ops, node.comparators, strict=True):
                right = self.expr(item)
                if isinstance(op, ast.Eq):
                    matched = left == right
                elif isinstance(op, ast.NotEq):
                    matched = left != right
                elif isinstance(op, ast.In):
                    matched = left in right
                elif isinstance(op, ast.NotIn):
                    matched = left not in right
                elif isinstance(op, ast.Lt):
                    matched = left < right
                elif isinstance(op, ast.LtE):
                    matched = left <= right
                elif isinstance(op, ast.Gt):
                    matched = left > right
                else:
                    matched = left >= right
                if not matched:
                    value = False
                    break
                left = right
        elif isinstance(node, ast.IfExp):
            value = self.expr(node.body if self.expr(node.test) else node.orelse)
        elif isinstance(node, ast.Call):
            values = [self.expr(item) for item in node.args]
            name = node.func.id
            if name == "call" and len(values) == 2 and type(values[1]) is dict:
                value = self.dispatch(*values)
            elif (
                name == "get"
                and len(values) in (2, 3)
                and type(values[0]) is dict
                and type(values[1]) is str
            ):
                value = values[0].get(values[1], values[2] if len(values) == 3 else None)
            elif name == "len" and len(values) == 1 and type(values[0]) in (list, dict, str):
                value = len(values[0])
            elif (
                name == "append"
                and len(values) == 2
                and type(values[0]) is list
                and len(values[0]) < 256
            ):
                value = [*values[0], values[1]]
            elif (
                name == "range"
                and len(values) == 1
                and type(values[0]) is int
                and 0 <= values[0] <= self.iterations
            ):
                value = list(range(values[0]))
            else:
                raise ScriptError("script_helper_arguments_rejected")
        else:
            raise ScriptError("script_expression_rejected")
        encode(value, self.max_value_bytes)
        return value

    def tick(self):
        self.remaining -= 1
        if self.remaining < 0:
            raise ScriptError("script_steps_exhausted")

    def block(self, statements):
        for node in statements:
            self.tick()
            if isinstance(node, ast.Return):
                return True, self.expr(node.value) if node.value else None
            if isinstance(node, ast.Assign):
                self.env[node.targets[0].id] = self.expr(node.value)
            elif isinstance(node, ast.Expr):
                self.expr(node.value)
            elif isinstance(node, ast.If):
                done, value = self.block(node.body if self.expr(node.test) else node.orelse)
                if done:
                    return done, value
            elif isinstance(node, ast.For):
                items = self.expr(node.iter)
                if type(items) is not list or len(items) > self.iterations:
                    raise ScriptError("script_iterations_exhausted")
                for item in items:
                    self.iterations -= 1
                    if self.iterations < 0:
                        raise ScriptError("script_iterations_exhausted")
                    self.env[node.target.id] = item
                    done, value = self.block(node.body)
                    if done:
                        return done, value
        return False, None

    def run(self):
        try:
            done, value = self.block(self.body)
        except (TypeError, KeyError, IndexError, OverflowError, RecursionError):
            raise ScriptError("script_data_invalid") from None
        if not done:
            raise ScriptError("script_return_required")
        return value
