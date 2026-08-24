from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS loopback_effects(
  logical_key TEXT PRIMARY KEY,
  canonical_request BLOB NOT NULL,
  request_hash TEXT NOT NULL,
  effect_id TEXT UNIQUE NOT NULL,
  result BLOB NOT NULL,
  result_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loopback_effect_calls(
  logical_key TEXT PRIMARY KEY,
  calls INTEGER NOT NULL CHECK(calls > 0)
);
"""


@dataclass(frozen=True)
class LoopbackEffectAdapterFactory:
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
        # The loopback store holds every effect it performed under its logical
        # key, so a row that is absent is an authoritative absence.
        return True

    def open(self) -> LoopbackEffectAdapter:
        database_path = self.database_path.resolve()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(database_path)) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
        return LoopbackEffectAdapter(database_path, self.binding)


class LoopbackEffectAdapter:
    def __init__(self, database_path: Path, binding: EffectAdapterBinding) -> None:
        self._database_path = database_path
        self._binding = binding
        self._closed = False

    def readback(self, intent: EffectIntent) -> EffectReceipt | EffectAbsence:
        self._authorize_binding(intent)
        with (
            closing(sqlite3.connect(self._database_path, timeout=30.0)) as connection,
            connection,
        ):
            record = connection.execute(
                "SELECT canonical_request, request_hash, effect_id, result, result_hash "
                "FROM loopback_effects WHERE logical_key=?",
                (intent.binding.logical_key.value,),
            ).fetchone()
        if record is None:
            return EffectAbsence(intent.reference)
        self._verify_request(intent, bytes(record[0]), str(record[1]))
        result = EffectResult.from_durable_record(
            bytes(record[3]), EffectResult.payload_hash_type(str(record[4]))
        )
        return EffectReceipt(
            intent,
            EffectId(str(record[2])),
            result,
            ConfirmationSource.ADAPTER_READBACK,
        )

    def execute(self, intent: EffectIntent) -> PerformedEffect:
        self._authorize_binding(intent)
        with (
            closing(
                sqlite3.connect(self._database_path, timeout=30.0, isolation_level=None)
            ) as connection,
            connection,
        ):
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute(
                "SELECT canonical_request, request_hash, effect_id, result, result_hash "
                "FROM loopback_effects WHERE logical_key=?",
                (intent.binding.logical_key.value,),
            ).fetchone()
            if record is None:
                effect_id = EffectId(
                    hashlib.sha256(
                        intent.binding.logical_key.value.encode()
                    ).hexdigest()
                )
                result = EffectResult(intent.request.payload)
                connection.execute(
                    "INSERT INTO loopback_effects VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        intent.binding.logical_key.value,
                        intent.request.payload,
                        intent.request.request_hash.value,
                        effect_id.value,
                        result.payload,
                        result.payload_hash.value,
                    ),
                )
                connection.execute(
                    "INSERT INTO loopback_effect_calls VALUES(?, 1)",
                    (intent.binding.logical_key.value,),
                )
                connection.commit()
                return PerformedEffect(effect_id, result)
            self._verify_request(intent, bytes(record[0]), str(record[1]))
            connection.commit()
            return PerformedEffect(
                EffectId(str(record[2])),
                EffectResult.from_durable_record(
                    bytes(record[3]), EffectResult.payload_hash_type(str(record[4]))
                ),
            )

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
            raise RuntimeError("loopback effect adapter is closed")

    @staticmethod
    def _verify_request(
        intent: EffectIntent, stored_request: bytes, stored_hash: str
    ) -> None:
        if (
            stored_request != intent.request.payload
            or stored_hash != intent.request.request_hash.value
        ):
            raise EffectIntentMismatch(
                "loopback logical key already belongs to another exact request"
            )
