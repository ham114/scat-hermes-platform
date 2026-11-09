"""Private stdio MCP server for one trusted application identity."""

from __future__ import annotations

import json
from collections.abc import Callable

from mcp import types
from mcp.server import Server

from application.context import ExecutionContext
from application.readonly import ReadOnlyApplication


def create_server(
    application: ReadOnlyApplication,
    context_provider: Callable[[], ExecutionContext],
    *,
    synthetic=False,
) -> Server:
    server = Server("SCAT readonly SYNTHETIC" if synthetic else "SCAT readonly")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=name,
                description=spec.description,
                inputSchema=spec.schema.model_json_schema(),
                annotations=types.ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            )
            for name, spec in application.specs.items()
        ]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        context = context_provider()
        result = await application.invoke(name, arguments, context=context)
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text", text=json.dumps(result, ensure_ascii=False, default=str)
                )
            ],
            isError=result.get("status") not in {"ok", "not_found"},
        )

    return server
