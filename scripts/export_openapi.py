"""Export the FastAPI OpenAPI document consumed by the Vue client."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def remove_retired_routes(document, current):
    """Incremental exports must remove retired owned operations, too."""
    verbs = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
    for path, route in list(document["paths"].items()):
        removed = False
        for method, operation in list(route.items()):
            if (
                method in verbs
                and set(operation.get("tags", [])) & {"Memory", "Visualizations"}
                and method not in current["paths"].get(path, {})
            ):
                del route[method]
                removed = True
        if removed and not verbs.intersection(route):
            del document["paths"][path]
    schemas = document.get("components", {}).get("schemas", {})

    def refs(value):
        if isinstance(value, dict):
            reference = value.get("$ref", "")
            if reference.startswith("#/components/schemas/"):
                yield reference.rsplit("/", 1)[1]
            for child in value.values():
                yield from refs(child)
        elif isinstance(value, list):
            for child in value:
                yield from refs(child)

    reachable = set(refs(document["paths"]))
    pending = list(reachable)
    while pending:
        for name in refs(schemas.get(pending.pop(), {})):
            if name not in reachable:
                reachable.add(name)
                pending.append(name)
    for name in ("PreferenceEdit", "PreferenceList", "PreferenceRecord"):
        if name not in reachable:
            schemas.pop(name, None)


def main() -> None:
    from fastapi import FastAPI

    from service.memory_api import router as memory_router
    from service.visualization_api import router as visualization_router

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hermes",
        action="store_true",
        help="refresh visualization, memory and shared analysis contracts, retain the checked-in legacy API paths",
    )
    args = parser.parse_args()
    target = Path(__file__).resolve().parents[1] / "frontend" / "src" / "api" / "openapi.json"
    if args.hermes:
        document = json.loads(target.read_text(encoding="utf-8"))
    else:
        from service import app

        document = app.openapi()
    chart_app = FastAPI()
    chart_app.include_router(visualization_router)
    chart_app.include_router(memory_router)
    hermes = chart_app.openapi()
    remove_retired_routes(document, hermes)
    for path, route in hermes["paths"].items():
        if path.startswith("/api/v1/hermes"):
            document["paths"].setdefault(path, {}).update(route)
        elif path not in document["paths"]:
            document["paths"][path] = route
    for name, definition in hermes.get("components", {}).get("schemas", {}).items():
        existing = document["components"]["schemas"].get(name)
        if not args.hermes and existing is not None and existing != definition:
            raise ValueError("OpenAPI schema collision: " + name)
        document["components"]["schemas"][name] = definition
    target.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(target)


if __name__ == "__main__":
    main()
