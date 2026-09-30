"""A small JSON Schema checker for model responses.

The adapter checks every response against the schema the caller passed, whichever provider
produced it. Only the subset that structured-output APIs accept is supported: ``type``, ``enum``,
``const``, ``properties``, ``required``, ``additionalProperties: false``, ``items``, ``anyOf``.
Annotation keywords are ignored. Any other keyword raises ``ValueError``, so a schema can never be
silently half-checked.
"""

from __future__ import annotations

from typing import Any

_ANNOTATIONS = {"description", "title", "$schema", "default", "examples", "format"}
_SUPPORTED = {
    "type",
    "enum",
    "const",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "anyOf",
} | _ANNOTATIONS


def _type_ok(value: Any, name: str) -> bool:
    if name == "null":
        return value is None
    if name == "boolean":
        return isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if name == "string":
        return isinstance(value, str)
    if name == "array":
        return isinstance(value, list)
    if name == "object":
        return isinstance(value, dict)
    raise ValueError(f"unsupported schema type {name!r}")


def errors(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Every way ``value`` breaks ``schema``, as readable messages. Empty when it conforms."""
    unknown = set(schema) - _SUPPORTED
    if unknown:
        raise ValueError(f"unsupported schema keyword(s) at {path}: {sorted(unknown)}")

    if "anyOf" in schema:
        branches = [errors(value, branch, path) for branch in schema["anyOf"]]
        if not any(not b for b in branches):
            return [f"{path}: matches none of the allowed alternatives"]
        return []

    found: list[str] = []
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_type_ok(value, n) for n in names):
            return [f"{path}: expected {'|'.join(names)}, got {type(value).__name__}"]
    if "const" in schema and value != schema["const"]:
        found.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        found.append(f"{path}: {value!r} is not one of {schema['enum']}")

    if isinstance(value, dict):
        props: dict[str, Any] = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                found.append(f"{path}.{name}: missing")
        if schema.get("additionalProperties") is False:
            for name in sorted(set(value) - set(props)):
                found.append(f"{path}.{name}: not allowed")
        for name, sub in props.items():
            if name in value:
                found.extend(errors(value[name], sub, f"{path}.{name}"))
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            found.extend(errors(item, schema["items"], f"{path}[{i}]"))
    return found
