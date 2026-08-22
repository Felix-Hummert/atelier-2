from __future__ import annotations

import os
from pathlib import Path

from atelier2.contracts.agent_attempts import (
    RunnerGenerationBinding,
    RunnerTerminalEvidenceAckTombstone,
    RunnerTerminalEvidenceEnvelope,
    RunnerTerminalEvidenceHash,
)
from atelier2.contracts.runner_terminal_evidence_codec import (
    decode_runner_terminal_evidence_record,
    encode_runner_terminal_evidence_record,
)

_RECORD_NAME = "terminal-record"
_TEMPORARY_NAME = ".terminal-record.publish"


class RunnerJournal:
    """The Runner-owned, atomically published spool for one terminal fact."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._record = directory / _RECORD_NAME
        self._temporary = directory / _TEMPORARY_NAME
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._require_only_record()

    def publish(
        self, envelope: RunnerTerminalEvidenceEnvelope, bound_bytes: int
    ) -> None:
        """Publish the one terminal envelope this invocation ever gets.

        `bound_bytes` is the caller's manifest-declared journal capacity
        (`RunnerManifestV1.journal_bytes`). A tmpfs mount used to enforce that
        capacity by refusing an oversized write at the filesystem layer; on a
        durable volume nothing does, so this is the one place that fact is
        still kept true.
        """
        if self._record.exists():
            existing = self.readback(envelope.binding)
            if existing == envelope:
                return
            raise ValueError("runner journal already holds different terminal evidence")
        encoded = encode_runner_terminal_evidence_record(envelope)
        if len(encoded) > bound_bytes:
            raise ValueError("runner-journal-record-exceeds-bound")
        self._publish(encoded)

    def readback(
        self, binding: RunnerGenerationBinding
    ) -> RunnerTerminalEvidenceEnvelope | RunnerTerminalEvidenceAckTombstone:
        self._require_only_record()
        if not self._record.is_file():
            raise ValueError("runner-terminal-record-missing")
        record = decode_runner_terminal_evidence_record(self._record.read_bytes())
        if not isinstance(
            record, (RunnerTerminalEvidenceEnvelope, RunnerTerminalEvidenceAckTombstone)
        ):
            raise TypeError("runner-terminal-record-corrupt")
        if record.binding != binding:
            raise ValueError("runner terminal record binding differs")
        return record

    def acknowledge(
        self,
        envelope: RunnerTerminalEvidenceEnvelope,
        accepted_hash: RunnerTerminalEvidenceHash,
    ) -> RunnerTerminalEvidenceAckTombstone:
        record = self.readback(envelope.binding)
        if record != envelope:
            raise ValueError("runner ACK requires the retained exact envelope")
        expected = RunnerTerminalEvidenceHash.for_envelope(envelope)
        if accepted_hash != expected:
            raise ValueError("runner-ack-hash-mismatch")
        tombstone = RunnerTerminalEvidenceAckTombstone(
            envelope.binding, envelope.invocation_id, accepted_hash
        )
        self._publish(encode_runner_terminal_evidence_record(tombstone))
        return tombstone

    def release(
        self,
        binding: RunnerGenerationBinding,
        accepted_hash: RunnerTerminalEvidenceHash,
    ) -> None:
        record = self.readback(binding)
        if not isinstance(record, RunnerTerminalEvidenceAckTombstone):
            raise TypeError("runner-release-before-ack")
        if record.evidence_hash != accepted_hash:
            raise ValueError("runner RELEASE hash differs from ACK tombstone")
        self._record.unlink()
        self._sync_directory()

    def _publish(self, encoded: bytes) -> None:
        self._require_only_record()
        descriptor = os.open(
            self._temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC,
            0o600,
        )
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(self._temporary, self._record)
        self._sync_directory()

    def _require_only_record(self) -> None:
        names = {entry.name for entry in self._directory.iterdir()}
        allowed = {_RECORD_NAME}
        if names - allowed:
            raise ValueError("runner journal contains a forbidden record")

    def _sync_directory(self) -> None:
        descriptor = os.open(
            self._directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
