"""Workflow schemas stay acceptable to providers that enforce strict types."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

SCHEMA_DIRECTORY = Path(__file__).parents[2] / "workflows" / "schemas"

TYPE_SPECIFIC_KEYWORDS = {
    "array": ("items", "minItems", "maxItems", "uniqueItems", "contains"),
    "object": (
        "properties",
        "required",
        "additionalProperties",
        "minProperties",
    ),
    "string": ("minLength", "maxLength", "pattern", "format"),
    "number": ("minimum", "maximum", "multipleOf"),
}

DIRECT_SCHEMA_KEYWORDS = (
    "additionalItems",
    "additionalProperties",
    "contains",
    "contentSchema",
    "else",
    "if",
    "items",
    "not",
    "propertyNames",
    "then",
    "unevaluatedItems",
    "unevaluatedProperties",
)

MAPPING_SCHEMA_KEYWORDS = (
    "$defs",
    "definitions",
    "dependentSchemas",
    "patternProperties",
    "properties",
)

ARRAY_SCHEMA_KEYWORDS = ("allOf", "anyOf", "oneOf", "prefixItems")


def _escape_json_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _has_compatible_type(schema: Mapping[object, object], expected: str) -> bool:
    declared_type = schema.get("type")
    compatible_types = {expected}
    if expected == "number":
        compatible_types.add("integer")
    if isinstance(declared_type, str):
        return declared_type in compatible_types
    if isinstance(declared_type, list):
        return any(
            isinstance(candidate, str) and candidate in compatible_types
            for candidate in declared_type
        )
    return False


def _schema_children(schema: Mapping[object, object]) -> list[tuple[str, object]]:
    children = [
        (f"/{_escape_json_pointer_token(keyword)}", schema[keyword])
        for keyword in DIRECT_SCHEMA_KEYWORDS
        if keyword in schema
    ]
    for keyword in MAPPING_SCHEMA_KEYWORDS:
        child_schemas = schema.get(keyword)
        if isinstance(child_schemas, Mapping):
            children.extend(
                (
                    f"/{_escape_json_pointer_token(keyword)}/{_escape_json_pointer_token(name)}",
                    child,
                )
                for name, child in child_schemas.items()
                if isinstance(name, str)
            )
    for keyword in ARRAY_SCHEMA_KEYWORDS:
        child_schemas = schema.get(keyword)
        if isinstance(child_schemas, list):
            children.extend(
                (f"/{_escape_json_pointer_token(keyword)}/{index}", child)
                for index, child in enumerate(child_schemas)
            )
    dependencies = schema.get("dependencies")
    if isinstance(dependencies, Mapping):
        children.extend(
            (f"/dependencies/{_escape_json_pointer_token(name)}", child)
            for name, child in dependencies.items()
            if isinstance(name, str) and isinstance(child, (Mapping, bool))
        )
    return children


def _type_keyword_errors(subschema: object, pointer: str = "") -> list[str]:
    if isinstance(subschema, Mapping):
        errors = [
            f"{pointer or '/'}: {expected} keyword {keyword!r} needs a compatible "
            "type on the same subschema"
            for expected, keywords in TYPE_SPECIFIC_KEYWORDS.items()
            if not _has_compatible_type(subschema, expected)
            for keyword in keywords
            if keyword in subschema
        ]
        errors.extend(
            error
            for suffix, child in _schema_children(subschema)
            for error in _type_keyword_errors(child, f"{pointer}{suffix}")
        )
        return errors
    return []


def test_schema_annotations_do_not_become_subschemas() -> None:
    schema = {
        "type": "object",
        "examples": [{"required": ["a real property"]}],
        "default": {"required": ["another real property"]},
        "const": {"required": ["a constant property"]},
        "enum": [{"required": ["an enumerated property"]}],
    }

    assert _type_keyword_errors(schema) == []


def test_missing_type_reports_the_subschema_json_pointer() -> None:
    schema = {"allOf": [{"properties": {"findings": {"minItems": 1}}}]}

    errors = _type_keyword_errors(schema)

    assert any(error.startswith("/allOf/0:") for error in errors)
    assert any(error.startswith("/allOf/0/properties/findings:") for error in errors)


def test_workflow_schema_type_specific_keywords_declare_a_type() -> None:
    errors = [
        f"{schema_path.relative_to(SCHEMA_DIRECTORY.parents[1])}: {error}"
        for schema_path in sorted(SCHEMA_DIRECTORY.glob("*.json"))
        for error in _type_keyword_errors(json.loads(schema_path.read_text()))
    ]

    assert not errors, "\n".join(errors)
