"""Run private stdio MCP; --synthetic is only for isolated protocol tests."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


async def main(synthetic: bool, skill_tools: bool = False) -> None:
    from mcp.server.stdio import stdio_server

    from application.context import ExecutionContext
    from application.readonly import (
        READ_ONLY_TOOLS,
        ReadOnlyApplication,
        build_business_application,
    )
    from scat_mcp.server import create_server

    context = ExecutionContext(
        os.environ.get("ANALYSIS_SINGLE_USER_ID") or "scat-researcher", "mcp-readonly"
    )
    if synthetic and skill_tools:
        from application.skill_synthetic import build_synthetic_skill_application

        application = build_synthetic_skill_application()
    elif synthetic:

        def operation(name):
            async def execute(**kwargs):
                return {"status": "ok", "synthetic": True, "operation": name, "data": kwargs}

            return execute

        application = ReadOnlyApplication({name: operation(name) for name in READ_ONLY_TOOLS})
    else:
        application = build_business_application(skill_tools=skill_tools)
    server = create_server(application, lambda: context, synthetic=synthetic)
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await application.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--skill-tools", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    asyncio.run(main(args.synthetic, args.skill_tools))
