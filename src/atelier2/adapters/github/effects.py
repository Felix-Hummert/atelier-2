"""Publication and readback for the `open-pr` adapter operation.

The factory is an `EffectAdapterFactory`. This slice's destination is a
recorded fake platform: it can list every pull request it ever created, so a
missing marker is an authoritative absence rather than an unmatched search.
Live GitHub cannot prove that negative; that path is not composed here.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atelier2.adapters.github.marker import body_carries_request_hash, marker_line
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    ConfirmationSource,
    EffectAbsence,
    EffectAdapterBinding,
    EffectDestination,
    EffectId,
    EffectIntent,
    EffectIntentMismatch,
    EffectReceipt,
    EffectResult,
    PerformedEffect,
)

_SQLITE_LOCK_TIMEOUT_SECONDS = 30.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pull_requests(
  request_hash TEXT PRIMARY KEY,
  branch TEXT NOT NULL,
  pr_number INTEGER NOT NULL UNIQUE,
  body TEXT NOT NULL,
  effect_id TEXT UNIQUE NOT NULL,
  result BLOB NOT NULL,
  result_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pull_request_creates(
  request_hash TEXT PRIMARY KEY,
  creates INTEGER NOT NULL CHECK(creates > 0)
);
"""


@dataclass(frozen=True)
class RecordedPullRequest:
    """One pull request the fake platform recorded, as the adapter names it."""

    branch: str
    pr_number: int
    body: str
    request_hash: str


def _result_payload(branch: str, pr_number: int) -> bytes:
    return json.dumps(
        {"branch": branch, "pr_number": pr_number},
        separators=(",", ":"),
    ).encode("utf-8")


def _branch_for(request_hash: str) -> str:
    return f"atelier2-open-pr-{request_hash[:12]}"


def _row(record: Any) -> tuple[str, int, str, str, bytes, str] | None:
    if record is None:
        return None
    return (
        str(record[0]),
        int(record[1]),
        str(record[2]),
        str(record[3]),
        bytes(record[4]),
        str(record[5]),
    )


def _body_for(request: CanonicalRequest) -> str:
    try:
        tree = request.payload.decode("utf-8")
    except UnicodeDecodeError:
        tree = request.payload.hex()
    return f"{tree}\n\n{marker_line(request.request_hash.value)}\n"


@dataclass(frozen=True)
class GitHubEffectAdapterFactory:
    database_path: Path
    adapter_revision: AdapterRevision
    destination: EffectDestination

    @property
    def binding(self) -> EffectAdapterBinding:
        return EffectAdapterBinding(
            self.adapter_revision,
            self.destination,
            AdapterOperationalIdentity(str(self.database_path.resolve())),
        )

    @property
    def proves_absence(self) -> bool:
        # The fake platform lists every pull request it ever created, so a
        # missing marker is an authoritative absence (ADR 0010 §5).
        return True

    def open(self) -> GitHubEffectAdapter:
        database_path = self.database_path.resolve()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(database_path)) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
        return GitHubEffectAdapter(database_path, self.binding)

    def recorded_pull_requests(self) -> tuple[RecordedPullRequest, ...]:
        """Every pull request this fake has created, in number order."""
        database_path = self.database_path.resolve()
        if not database_path.is_file():
            return ()
        with closing(
            sqlite3.connect(database_path, timeout=_SQLITE_LOCK_TIMEOUT_SECONDS)
        ) as connection:
            rows = connection.execute(
                "SELECT branch, pr_number, body, request_hash "
                "FROM pull_requests ORDER BY pr_number"
            ).fetchall()
        return tuple(
            RecordedPullRequest(str(row[0]), int(row[1]), str(row[2]), str(row[3]))
            for row in rows
        )


class GitHubEffectAdapter:
    def __init__(
        self,
        database_path: Path,
        binding: EffectAdapterBinding,
    ) -> None:
        self._database_path = database_path
        self._binding = binding
        self._closed = False

    def readback(self, intent: EffectIntent) -> EffectReceipt | EffectAbsence:
        self._authorize_binding(intent)
        record = self._load(intent.request.request_hash.value)
        if record is None:
            return EffectAbsence(intent.reference)
        return self._receipt(intent, record)

    def execute(self, intent: EffectIntent) -> PerformedEffect:
        self._authorize_binding(intent)
        request_hash = intent.request.request_hash.value
        with (
            closing(
                sqlite3.connect(
                    self._database_path,
                    timeout=_SQLITE_LOCK_TIMEOUT_SECONDS,
                    isolation_level=None,
                )
            ) as connection,
            connection,
        ):
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("BEGIN IMMEDIATE")
            record = _row(
                connection.execute(
                    "SELECT branch, pr_number, body, effect_id, result, result_hash "
                    "FROM pull_requests WHERE request_hash=?",
                    (request_hash,),
                ).fetchone()
            )
            if record is not None:
                self._verify_recorded_request(intent, record)
                connection.commit()
                return PerformedEffect(
                    EffectId(record[3]),
                    EffectResult.from_durable_record(
                        record[4], EffectResult.payload_hash_type(record[5])
                    ),
                )
            pr_number = self._next_pr_number(connection)
            branch = _branch_for(request_hash)
            body = _body_for(intent.request)
            result = EffectResult(_result_payload(branch, pr_number))
            effect_id = EffectId(str(pr_number))
            connection.execute(
                "INSERT INTO pull_requests VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    request_hash,
                    branch,
                    pr_number,
                    body,
                    effect_id.value,
                    result.payload,
                    result.payload_hash.value,
                ),
            )
            connection.execute(
                "INSERT INTO pull_request_creates VALUES(?, 1)",
                (request_hash,),
            )
            connection.commit()
            return PerformedEffect(effect_id, result)

    def close(self) -> None:
        self._closed = True

    def _authorize_binding(self, intent: EffectIntent) -> None:
        self._require_open()
        if intent.binding.adapter_binding != self._binding:
            raise EffectIntentMismatch(
                "effect intent does not belong to this adapter binding"
            )

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("github effect adapter is closed")

    def _load(self, request_hash: str) -> tuple[str, int, str, str, bytes, str] | None:
        with (
            closing(
                sqlite3.connect(
                    self._database_path, timeout=_SQLITE_LOCK_TIMEOUT_SECONDS
                )
            ) as connection,
            connection,
        ):
            return _row(
                connection.execute(
                    "SELECT branch, pr_number, body, effect_id, result, result_hash "
                    "FROM pull_requests WHERE request_hash=?",
                    (request_hash,),
                ).fetchone()
            )

    def _receipt(
        self,
        intent: EffectIntent,
        record: tuple[str, int, str, str, bytes, str],
    ) -> EffectReceipt:
        self._verify_recorded_request(intent, record)
        result = EffectResult.from_durable_record(
            record[4], EffectResult.payload_hash_type(record[5])
        )
        return EffectReceipt(
            intent,
            EffectId(record[3]),
            result,
            ConfirmationSource.ADAPTER_READBACK,
        )

    @staticmethod
    def _verify_recorded_request(
        intent: EffectIntent, record: tuple[str, int, str, str, bytes, str]
    ) -> None:
        body = str(record[2])
        request_hash = intent.request.request_hash.value
        if not body_carries_request_hash(body, request_hash):
            raise EffectIntentMismatch(
                "recorded pull request does not carry this request's marker"
            )

    @staticmethod
    def _next_pr_number(connection: sqlite3.Connection) -> int:
        highest = connection.execute(
            "SELECT COALESCE(MAX(pr_number), 0) FROM pull_requests"
        ).fetchone()
        return int(highest[0]) + 1
