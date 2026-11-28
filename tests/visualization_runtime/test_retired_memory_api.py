"""Retired management routes must not survive incremental OpenAPI exports."""

from scripts.export_openapi import remove_retired_routes


def document():
    return {
        "paths": {
            "/api/v1/hermes/memory/chart-preferences": {
                "get": {
                    "tags": ["Memory"],
                    "schema": {"$ref": "#/components/schemas/PreferenceList"},
                },
                "post": {
                    "tags": ["Memory"],
                    "schema": {"$ref": "#/components/schemas/PreferenceEdit"},
                },
            },
            "/api/v1/hermes/threads": {"get": {"tags": ["Hermes"]}},
            "/api/v1/hermes/memory": {"get": {"tags": ["Memory"]}},
        },
        "components": {
            "schemas": {
                "PreferenceList": {"items": {"$ref": "#/components/schemas/PreferenceRecord"}},
                "PreferenceRecord": {"type": "object"},
                "PreferenceEdit": {"type": "object"},
                "Unrelated": {"type": "object"},
            }
        },
    }


def test_export_removes_retired_memory_operations_and_preserves_other_hermes_routes():
    value = document()
    current = {"paths": {"/api/v1/hermes/memory": {"get": {"tags": ["Memory"]}}}}
    remove_retired_routes(value, current)
    assert "/api/v1/hermes/memory/chart-preferences" not in value["paths"]
    assert "/api/v1/hermes/threads" in value["paths"]
    assert set(value["components"]["schemas"]) == {"Unrelated"}
    unchanged = repr(value)
    remove_retired_routes(value, current)
    assert repr(value) == unchanged


def test_export_keeps_a_schema_still_referenced_by_an_unrelated_api():
    value = document()
    value["paths"]["/legacy"] = {
        "get": {"tags": ["Legacy"], "schema": {"$ref": "#/components/schemas/PreferenceRecord"}}
    }
    remove_retired_routes(value, {"paths": {}})
    assert "PreferenceRecord" in value["components"]["schemas"]
    assert "PreferenceList" not in value["components"]["schemas"]
