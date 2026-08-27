"""Canonical request and result bytes for the two published effect operations."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, Self

from atelier2.contracts.effect_markers import commit_message

_SAFE_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_UNSAFE_BRANCH_FRAGMENTS = ("..", "@{", "//")


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _object(value: bytes, owner: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{owner} is not canonical JSON") from error
    if not isinstance(decoded, dict) or _canonical_json(decoded) != value:
        raise ValueError(f"{owner} is not one canonical JSON object")
    return decoded


def _fields(value: dict[str, Any], expected: frozenset[str], owner: str) -> None:
    if frozenset(value) != expected:
        raise ValueError(f"{owner} carries exactly {', '.join(sorted(expected))}")


@dataclass(frozen=True, slots=True)
class GitCommitIdentity:
    name: str
    email: str

    def __post_init__(self) -> None:
        if not self.name or any(character in self.name for character in "\r\n<>"):
            raise ValueError("a git identity name is nonempty and header-safe")
        if (
            not self.email
            or "@" not in self.email
            or any(character in self.email for character in "\r\n<> ")
        ):
            raise ValueError("a git identity email is nonempty and header-safe")

    def as_json(self) -> dict[str, str]:
        return {"email": self.email, "name": self.name}

    @classmethod
    def from_json(cls, value: object) -> Self:
        if not isinstance(value, dict) or set(value) != {"name", "email"}:
            raise ValueError("a git identity carries name and email")
        name = value["name"]
        email = value["email"]
        if not isinstance(name, str) or not isinstance(email, str):
            raise TypeError("a git identity name and email are text")
        return cls(name, email)


@dataclass(frozen=True, slots=True)
class HeadBranch:
    value: str

    def __post_init__(self) -> None:
        unsafe = (
            not self.value
            or len(self.value) > 240
            or _SAFE_BRANCH.fullmatch(self.value) is None
            or any(fragment in self.value for fragment in _UNSAFE_BRANCH_FRAGMENTS)
            or self.value.endswith(("/", ".", ".lock"))
            or any(
                part.startswith(".") or part.endswith(".lock")
                for part in self.value.split("/")
            )
        )
        if unsafe:
            raise ValueError(f"unsafe branch {self.value!r}")

    @property
    def full_ref(self) -> str:
        return f"refs/heads/{self.value}"


class QueueItemIdentity(Protocol):
    @property
    def value(self) -> str: ...


def head_branch_for_queue_item(item_id: QueueItemIdentity) -> HeadBranch:
    return HeadBranch(f"atelier2/work-item/{item_id.value}")


@dataclass(frozen=True, slots=True)
class OpenPullRequest:
    body: str
    head_branch: HeadBranch

    def canonical_bytes(self) -> bytes:
        return _canonical_json(
            {"body": self.body, "head_branch": self.head_branch.value}
        )

    @classmethod
    def from_canonical_bytes(cls, request: bytes) -> Self:
        value = _object(request, "open-pr request")
        _fields(value, frozenset(("body", "head_branch")), "open-pr request")
        body = value["body"]
        branch = value["head_branch"]
        if not isinstance(body, str) or not isinstance(branch, str):
            raise TypeError("open-pr body and head_branch are text")
        return cls(body, HeadBranch(branch))


@dataclass(frozen=True, slots=True)
class PushAtelierCommit:
    attempt_id: str
    candidate_tree: str
    base_commit: str
    head_branch: HeadBranch
    author: GitCommitIdentity
    committer: GitCommitIdentity
    completed_at: str

    def __post_init__(self) -> None:
        if len(self.attempt_id) != 64 or any(
            c not in "0123456789abcdef" for c in self.attempt_id
        ):
            raise ValueError("a push request attempt id is a SHA-256 hash")
        lengths = {len(self.candidate_tree), len(self.base_commit)}
        if lengths not in ({40}, {64}) or any(
            any(character not in "0123456789abcdef" for character in value)
            for value in (self.candidate_tree, self.base_commit)
        ):
            raise ValueError("a push request base and tree use one git object format")
        try:
            datetime.strptime(self.completed_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )
        except ValueError as error:
            raise ValueError("a push completion timestamp is RFC 3339 UTC") from error

    @property
    def object_format(self) -> str:
        return "sha1" if len(self.base_commit) == 40 else "sha256"

    def canonical_bytes(self) -> bytes:
        return _canonical_json(
            {
                "attempt_id": self.attempt_id,
                "author": self.author.as_json(),
                "base_commit": self.base_commit,
                "candidate_tree": self.candidate_tree,
                "committer": self.committer.as_json(),
                "completed_at": self.completed_at,
                "head_branch": self.head_branch.value,
            }
        )

    @classmethod
    def from_canonical_bytes(cls, request: bytes) -> Self:
        value = _object(request, "push request")
        _fields(
            value,
            frozenset(
                (
                    "attempt_id",
                    "author",
                    "base_commit",
                    "candidate_tree",
                    "committer",
                    "completed_at",
                    "head_branch",
                )
            ),
            "push request",
        )
        text_fields = (
            "attempt_id",
            "base_commit",
            "candidate_tree",
            "completed_at",
            "head_branch",
        )
        if any(not isinstance(value[field], str) for field in text_fields):
            raise ValueError("push request identity, objects, time and branch are text")
        return cls(
            value["attempt_id"],
            value["candidate_tree"],
            value["base_commit"],
            HeadBranch(value["head_branch"]),
            GitCommitIdentity.from_json(value["author"]),
            GitCommitIdentity.from_json(value["committer"]),
            value["completed_at"],
        )

    def commit_bytes(self, request_hash: str) -> bytes:
        completed = datetime.strptime(self.completed_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
        timestamp = int(completed.timestamp())
        lines = (
            f"tree {self.candidate_tree}\n"
            f"parent {self.base_commit}\n"
            f"author {self.author.name} <{self.author.email}> {timestamp} +0000\n"
            f"committer {self.committer.name} <{self.committer.email}> {timestamp} +0000\n"
            "\n"
            f"{commit_message(self.attempt_id, request_hash)}"
        )
        return lines.encode("utf-8")

    def expected_commit_oid(self, request_hash: str) -> str:
        content = self.commit_bytes(request_hash)
        object_bytes = f"commit {len(content)}\0".encode("ascii") + content
        algorithm = hashlib.sha1 if self.object_format == "sha1" else hashlib.sha256
        return algorithm(object_bytes).hexdigest()


@dataclass(frozen=True, slots=True)
class PushAtelierCommitReceipt:
    remote_identity: str
    full_ref: str
    commit_oid: str
    parent: str
    candidate_tree: str
    branch: str
    author: GitCommitIdentity
    committer: GitCommitIdentity

    def result_bytes(self) -> bytes:
        return _canonical_json(
            {
                "author": self.author.as_json(),
                "branch": self.branch,
                "candidate_tree": self.candidate_tree,
                "commit_oid": self.commit_oid,
                "committer": self.committer.as_json(),
                "full_ref": self.full_ref,
                "parent": self.parent,
                "remote_identity": self.remote_identity,
            }
        )
