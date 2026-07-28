"""Dependency-free, fail-closed validation for bounded tool arguments.

Only the JSON-schema keywords used by current tool contracts are supported.
Future adapters must extend this module explicitly instead of relying on a
keyword the runtime would silently ignore.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


_JSON_TYPES = {
    "array",
    "boolean",
    "integer",
    "null",
    "number",
    "object",
    "string",
}
_SUPPORTED_KEYS = {
    "additionalProperties",
    "default",
    "description",
    "enum",
    "examples",
    "items",
    "maximum",
    "minimum",
    "properties",
    "required",
    "title",
    "type",
}


@dataclass(frozen=True, slots=True)
class SchemaValidationIssue:
    """One stable, model-readable argument validation failure."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


def validate_schema_definition(schema: dict[str, Any]) -> None:
    """Reject malformed or unsupported tool schemas at registration time."""

    _check_schema(schema, path="$")


def validate_tool_arguments(
    arguments: dict[str, Any],
    schema: dict[str, Any],
    *,
    max_issues: int = 8,
) -> tuple[SchemaValidationIssue, ...]:
    """Validate arguments without coercion and return bounded issues."""

    if max_issues <= 0:
        raise ValueError("max_issues must be positive")
    issues: list[SchemaValidationIssue] = []
    _validate(arguments, schema, path="$", issues=issues, limit=max_issues)
    return tuple(issues)


def _check_schema(schema: Any, *, path: str) -> None:
    if not isinstance(schema, dict):
        raise ValueError(f"{path}: schema must be an object")
    unknown = sorted(set(schema).difference(_SUPPORTED_KEYS))
    if unknown:
        raise ValueError(f"{path}: unsupported schema keywords: {', '.join(unknown)}")

    declared = schema.get("type")
    if declared is not None:
        declared_types = declared if isinstance(declared, list) else [declared]
        if not declared_types or any(
            not isinstance(item, str) or item not in _JSON_TYPES
            for item in declared_types
        ):
            raise ValueError(f"{path}.type: expected supported JSON type or list")
        if len(declared_types) != len(set(declared_types)):
            raise ValueError(f"{path}.type: duplicate JSON types are not allowed")

    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise ValueError(f"{path}.properties: expected object")
        for name, child in properties.items():
            if not isinstance(name, str) or not name:
                raise ValueError(f"{path}.properties: names must be non-empty")
            _check_schema(child, path=f"{path}.properties.{name}")

    required = schema.get("required")
    if required is not None:
        if not isinstance(required, list) or any(
            not isinstance(item, str) or not item for item in required
        ):
            raise ValueError(f"{path}.required: expected property-name list")
        if len(required) != len(set(required)):
            raise ValueError(f"{path}.required: duplicate property names")

    additional = schema.get("additionalProperties")
    if additional is not None and not isinstance(additional, bool):
        _check_schema(additional, path=f"{path}.additionalProperties")

    items = schema.get("items")
    if items is not None:
        _check_schema(items, path=f"{path}.items")

    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or not enum):
        raise ValueError(f"{path}.enum: expected non-empty list")

    for keyword in ("minimum", "maximum"):
        value = schema.get(keyword)
        if value is not None and not _is_number(value):
            raise ValueError(f"{path}.{keyword}: expected number")


def _validate(
    value: Any,
    schema: dict[str, Any],
    *,
    path: str,
    issues: list[SchemaValidationIssue],
    limit: int,
) -> None:
    if len(issues) >= limit:
        return
    declared = schema.get("type")
    if declared is not None:
        declared_types = declared if isinstance(declared, list) else [declared]
        if not any(_matches_type(value, item) for item in declared_types):
            issues.append(
                SchemaValidationIssue(
                    path,
                    "expected "
                    + " or ".join(declared_types)
                    + f", got {_type_name(value)}",
                )
            )
            return

    if "enum" in schema and not any(
        _json_equal(value, item) for item in schema["enum"]
    ):
        issues.append(SchemaValidationIssue(path, "value is not in enum"))
    if len(issues) >= limit:
        return

    if isinstance(value, dict):
        _validate_object(value, schema, path=path, issues=issues, limit=limit)
    elif isinstance(value, list):
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                _validate(
                    item,
                    item_schema,
                    path=f"{path}[{index}]",
                    issues=issues,
                    limit=limit,
                )
                if len(issues) >= limit:
                    return
    elif _is_number(value):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < minimum:
            issues.append(SchemaValidationIssue(path, f"must be >= {minimum}"))
        if maximum is not None and value > maximum:
            issues.append(SchemaValidationIssue(path, f"must be <= {maximum}"))


def _validate_object(
    value: dict[str, Any],
    schema: dict[str, Any],
    *,
    path: str,
    issues: list[SchemaValidationIssue],
    limit: int,
) -> None:
    properties = schema.get("properties", {})
    for name in schema.get("required", ()):
        if name not in value:
            issues.append(
                SchemaValidationIssue(
                    _property_path(path, name),
                    "required property is missing",
                )
            )
            if len(issues) >= limit:
                return

    additional = schema.get("additionalProperties", True)
    for name, item in value.items():
        child_path = _property_path(path, name)
        if name in properties:
            _validate(
                item,
                properties[name],
                path=child_path,
                issues=issues,
                limit=limit,
            )
        elif additional is False:
            issues.append(
                SchemaValidationIssue(child_path, "additional property is not allowed")
            )
        elif isinstance(additional, dict):
            _validate(
                item,
                additional,
                path=child_path,
                issues=issues,
                limit=limit,
            )
        if len(issues) >= limit:
            return


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return _is_number(value)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _json_equal(left: Any, right: Any) -> bool:
    try:
        return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)
    except (TypeError, ValueError):
        return left == right and type(left) is type(right)


def _property_path(path: str, name: str) -> str:
    return f"{path}.{name}" if name.isidentifier() else f"{path}[{name!r}]"
