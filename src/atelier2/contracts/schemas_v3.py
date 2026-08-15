"""What a document published as a `schema` revision must be to mean anything.

A V3 document binds a node output to `outputs[*].schema`, and the resolution of
that reference proves the registry carries the pinned revision. It cannot prove
the revision says anything: bytes published under the name `schema` are a schema
only because someone called them one. This module is the proof that was missing,
and it is deliberately a pure function over bytes so that both the reference
resolution and, later, the instance evaluation ask exactly one owner.

The profile is a closed subset of JSON Schema Draft 2020-12, and every bound in
it exists to keep evaluation decidable, local and cheap:

- the document is bounded in bytes, in container depth and in value count, so a
  published revision cannot cost unbounded work to read;
- `$id`, `$anchor`, `$dynamicAnchor` and `$dynamicRef` are refused, because each
  one moves the meaning of a reference somewhere the pinned revision hash does
  not cover;
- a `$ref` is local or it is refused, so what a schema means travels with its
  own bytes and never over the network;
- `format` stays the draft's annotation and is never an assertion, so a schema
  that needs format validation for safety is not expressible here rather than
  silently unenforced.

Retrieval is off by construction rather than by trust: every evaluation runs
against a registry whose only retrieval path is `refuse_retrieval`, which raises.
The forbidden-vocabulary scan runs first, so a document naming a non-local
reference is refused by name before any evaluator sees it and nothing ever
reaches that seam.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import NoReturn

from jsonschema import Draft202012Validator
from jsonschema_specifications import REGISTRY as SPECIFICATION_REGISTRY
from referencing import Registry
from referencing.jsonschema import Schema

MAXIMUM_SCHEMA_DOCUMENT_BYTES = 16_384
MAXIMUM_SCHEMA_CONTAINER_DEPTH = 32
MAXIMUM_SCHEMA_VALUES = 4_096

FORBIDDEN_KEYWORDS = ("$id", "$anchor", "$dynamicAnchor", "$dynamicRef")
_LOCAL_REFERENCE_PREFIX = "#"
_BYTE_ORDER_MARK = "﻿"


class SchemaDocumentRefusal(StrEnum):
    """Every named way a published `schema` revision fails this closed profile."""

    DOCUMENT_TOO_LARGE = "document-too-large"
    DOCUMENT_NOT_UTF8 = "document-not-utf8"
    DOCUMENT_CARRIES_BYTE_ORDER_MARK = "document-carries-byte-order-mark"
    DOCUMENT_NOT_JSON = "document-not-json"
    NON_CANONICAL_NUMBER = "non-canonical-number"
    DUPLICATE_OBJECT_KEY = "duplicate-object-key"
    DOCUMENT_TOO_DEEP = "document-too-deep"
    TOO_MANY_VALUES = "too-many-values"
    FORBIDDEN_KEYWORD = "forbidden-keyword"
    NONLOCAL_REFERENCE = "nonlocal-reference"
    NOT_A_SCHEMA = "not-a-schema"


@dataclass(frozen=True, slots=True)
class SchemaAccepted:
    """The document is a Draft 2020-12 schema inside this profile."""

    schema: Schema


@dataclass(frozen=True, slots=True)
class SchemaRefused:
    """One named refusal, with the exact thing it is about when there is one."""

    refusal: SchemaDocumentRefusal
    subject: str | None = None

    def __str__(self) -> str:
        named = "" if self.subject is None else f": {self.subject}"
        return f"{self.refusal.value}{named}"


type SchemaDocumentVerdict = SchemaAccepted | SchemaRefused


class SchemaRetrievalAttempted(Exception):
    """A schema evaluation reached for a resource outside its own bytes."""


def refuse_retrieval(uri: str) -> NoReturn:
    """The only retrieval path any schema evaluation has, and it never returns."""
    raise SchemaRetrievalAttempted(uri)


def schema_registry() -> Registry[Schema]:
    """The bundled specifications, and nothing else reachable."""
    return SPECIFICATION_REGISTRY.combine(Registry(retrieve=refuse_retrieval))


def read_schema_document(document: bytes) -> SchemaDocumentVerdict:
    """Whether these exact published bytes are a schema this product can enforce."""
    if len(document) > MAXIMUM_SCHEMA_DOCUMENT_BYTES:
        return SchemaRefused(
            SchemaDocumentRefusal.DOCUMENT_TOO_LARGE,
            f"{len(document)} bytes exceeds {MAXIMUM_SCHEMA_DOCUMENT_BYTES}",
        )
    try:
        text = document.decode("utf-8")
    except UnicodeDecodeError as broken:
        return SchemaRefused(SchemaDocumentRefusal.DOCUMENT_NOT_UTF8, broken.reason)
    if text.startswith(_BYTE_ORDER_MARK):
        return SchemaRefused(SchemaDocumentRefusal.DOCUMENT_CARRIES_BYTE_ORDER_MARK)
    decoded = _decoded_json(text)
    if isinstance(decoded, SchemaRefused):
        return decoded
    # The cheap bounds and the closed vocabulary run before anything reads the
    # document as a schema, so hostile input is refused by size and by keyword
    # rather than by an evaluator walking it.
    scanned = _scanned_vocabulary(decoded.value)
    if scanned is not None:
        return scanned
    if not isinstance(decoded.value, (bool, dict)):
        return SchemaRefused(
            SchemaDocumentRefusal.NOT_A_SCHEMA,
            f"a schema is an object or a boolean, not {type(decoded.value).__name__}",
        )
    return _validated_against_the_draft(decoded.value)


class _NonCanonicalNumber(Exception):
    def __init__(self, literal: str) -> None:
        super().__init__(literal)
        self.literal = literal


class _DuplicateObjectKey(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _refuse_constant(literal: str) -> NoReturn:
    raise _NonCanonicalNumber(literal)


def _refuse_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateObjectKey(key)
        seen[key] = value
    return seen


@dataclass(frozen=True, slots=True)
class _Decoded:
    """One decoded JSON value, before anything has claimed it is a schema."""

    value: object


def _decoded_json(text: str) -> _Decoded | SchemaRefused:
    try:
        value: object = json.loads(
            text,
            parse_constant=_refuse_constant,
            object_pairs_hook=_refuse_duplicate_keys,
        )
    except _NonCanonicalNumber as refused:
        return SchemaRefused(
            SchemaDocumentRefusal.NON_CANONICAL_NUMBER, refused.literal
        )
    except _DuplicateObjectKey as refused:
        return SchemaRefused(SchemaDocumentRefusal.DUPLICATE_OBJECT_KEY, refused.key)
    except json.JSONDecodeError as broken:
        return SchemaRefused(SchemaDocumentRefusal.DOCUMENT_NOT_JSON, broken.msg)
    return _Decoded(value)


def _scanned_vocabulary(schema: object) -> SchemaRefused | None:
    """The closed-vocabulary and locality walk, which also bounds depth and size."""
    counted = 0
    pending: list[tuple[object, int]] = [(schema, 1)]
    while pending:
        value, depth = pending.pop()
        counted += 1
        if counted > MAXIMUM_SCHEMA_VALUES:
            return SchemaRefused(
                SchemaDocumentRefusal.TOO_MANY_VALUES,
                f"more than {MAXIMUM_SCHEMA_VALUES} JSON values",
            )
        if not isinstance(value, (dict, list)):
            continue
        if depth > MAXIMUM_SCHEMA_CONTAINER_DEPTH:
            return SchemaRefused(
                SchemaDocumentRefusal.DOCUMENT_TOO_DEEP,
                f"more than {MAXIMUM_SCHEMA_CONTAINER_DEPTH} container levels",
            )
        if isinstance(value, list):
            pending.extend((entry, depth + 1) for entry in value)
            continue
        refused = _refused_keyword(value)
        if refused is not None:
            return refused
        pending.extend((entry, depth + 1) for entry in value.values())
    return None


def _refused_keyword(value: dict[str, object]) -> SchemaRefused | None:
    for keyword in FORBIDDEN_KEYWORDS:
        if keyword in value:
            return SchemaRefused(SchemaDocumentRefusal.FORBIDDEN_KEYWORD, keyword)
    reference = value.get("$ref")
    if isinstance(reference, str) and not reference.startswith(_LOCAL_REFERENCE_PREFIX):
        return SchemaRefused(SchemaDocumentRefusal.NONLOCAL_REFERENCE, reference)
    return None


def _validated_against_the_draft(schema: Schema) -> SchemaDocumentVerdict:
    validator = Draft202012Validator(
        Draft202012Validator.META_SCHEMA, registry=schema_registry()
    )
    for error in validator.iter_errors(schema):
        return SchemaRefused(SchemaDocumentRefusal.NOT_A_SCHEMA, error.message)
    return SchemaAccepted(schema)
