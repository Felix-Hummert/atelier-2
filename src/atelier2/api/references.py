from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

from atelier2.contracts.hashing import SHA256_HEX_DIGEST
from atelier2.contracts.runs import RunId, WorkflowRevisionHash

MAX_SIGNED_INT64 = 9_223_372_036_854_775_807
# The wire's own bound: no durable owner caps how many roles one run binds, so
# the edge decides it, once, rather than twice in two request shapes.
MAXIMUM_RUN_AGENT_BINDINGS = 100
# The wire's own bound: a published V3 node preview carries a start of the
# authored instruction, never the instruction. The durable owner bounds the
# whole instruction in UTF-8 bytes; this glance is a character count the
# edge decides once, so two shapes cannot pick two lengths.
MAXIMUM_NODE_INSTRUCTION_PREVIEW_CHARACTERS = 120
# Wire-owned: a validation loc and its reason have no durable owner, so the
# problem object decides the glance once.
MAXIMUM_INVALID_FIELD_PATH_CHARACTERS = 256
MAXIMUM_INVALID_FIELD_REASON_CHARACTERS = 512
SHA256_HASH_PATTERN = f"^{SHA256_HEX_DIGEST.pattern}$"
REVISION_HASH_PATTERN = SHA256_HASH_PATTERN
PUBLIC_RUN_REFERENCE_PATTERN = r"^run1\.[A-Za-z0-9_-]+$"
EVENT_CURSOR_PATTERN = r"^event1\.[A-Za-z0-9_-]+\.[1-9][0-9]*$"
_PUBLIC_REFERENCE_PREFIX = "run1."
_EVENT_CURSOR_PREFIX = "event1."
_UNPADDED_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")
_POSITIVE_DECIMAL = re.compile(r"[1-9][0-9]*")


class InvalidPublicRunReference(ValueError):
    """A public run reference is malformed or not canonically encoded."""


class InvalidEventCursor(ValueError):
    """An event cursor is malformed or outside the durable sequence range."""


class InvalidRevisionHash(ValueError):
    """A workflow revision hash is not an exact lowercase SHA-256 digest."""


@dataclass(frozen=True)
class EventCursor:
    run_id: RunId
    sequence: int

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or not 1 <= self.sequence <= MAX_SIGNED_INT64:
            raise InvalidEventCursor("event sequence must be a positive signed int64")


def encode_public_run_reference(run_id: RunId) -> str:
    try:
        payload = run_id.value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InvalidPublicRunReference("run id is not exact UTF-8") from error
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return _PUBLIC_REFERENCE_PREFIX + encoded


def decode_public_run_reference(reference: str) -> RunId:
    if not reference.startswith(_PUBLIC_REFERENCE_PREFIX):
        raise InvalidPublicRunReference("public run reference has the wrong version")
    encoded = reference.removeprefix(_PUBLIC_REFERENCE_PREFIX)
    if _UNPADDED_BASE64URL.fullmatch(encoded) is None:
        raise InvalidPublicRunReference(
            "public run reference is not canonical base64url"
        )
    try:
        payload = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
        run_id = RunId(payload.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise InvalidPublicRunReference(
            "public run reference has invalid payload"
        ) from error
    if encode_public_run_reference(run_id) != reference:
        raise InvalidPublicRunReference("public run reference is not canonical")
    return run_id


def encode_event_cursor(run_id: RunId, sequence: int) -> str:
    cursor = EventCursor(run_id, sequence)
    encoded_run_id = encode_public_run_reference(cursor.run_id).removeprefix(
        _PUBLIC_REFERENCE_PREFIX
    )
    return f"{_EVENT_CURSOR_PREFIX}{encoded_run_id}.{cursor.sequence}"


def parse_event_cursor(value: str) -> EventCursor:
    if not value.startswith(_EVENT_CURSOR_PREFIX):
        raise InvalidEventCursor("event cursor has the wrong version")
    body = value.removeprefix(_EVENT_CURSOR_PREFIX)
    try:
        encoded_run_id, encoded_sequence = body.rsplit(".", maxsplit=1)
    except ValueError as error:
        raise InvalidEventCursor("event cursor fields are missing") from error
    if _POSITIVE_DECIMAL.fullmatch(encoded_sequence) is None:
        raise InvalidEventCursor("event cursor sequence is not canonical")
    sequence = int(encoded_sequence)
    if sequence > MAX_SIGNED_INT64:
        raise InvalidEventCursor("event cursor sequence exceeds signed int64")
    try:
        run_id = decode_public_run_reference(_PUBLIC_REFERENCE_PREFIX + encoded_run_id)
    except InvalidPublicRunReference as error:
        raise InvalidEventCursor("event cursor run id is invalid") from error
    cursor = EventCursor(run_id, sequence)
    if encode_event_cursor(cursor.run_id, cursor.sequence) != value:
        raise InvalidEventCursor("event cursor is not canonical")
    return cursor


def parse_revision_hash(value: str) -> WorkflowRevisionHash:
    if SHA256_HEX_DIGEST.fullmatch(value) is None:
        raise InvalidRevisionHash("revision hash must be lowercase SHA-256")
    return WorkflowRevisionHash(value)


def decode_canonical_base64(value: str) -> bytes:
    if any(character.isspace() for character in value):
        raise ValueError("base64 must contain no whitespace")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("base64 must use the standard alphabet and padding") from error
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("base64 encoding is not canonical")
    return decoded


def encode_canonical_base64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")
