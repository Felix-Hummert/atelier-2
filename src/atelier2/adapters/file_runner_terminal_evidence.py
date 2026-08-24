"""Serve's read side of a Runner's retained terminal fact (`#540` Kind #585).

A Runner journals its one terminal fact and then tries to deliver it to Core
over the session; when Core (Serve) is restarted mid-session, that delivery
never lands and the Attempt stands armed forever. The launcher -- the only
process that can read the per-Attempt journal volume -- copies that record
verbatim into the Attempt's handoff directory before it would ever be removed
(`atelier2.host.runner_launcher`). This adapter is how Serve reads it back on
its own restart and converges the Attempt over
`atelier2.application.converge_runner_terminal_evidence`.

The seam is a one-way file copy, not a second reader of the journal volume:
Serve never holds carrier authority (ADR 0009 sec. 2), so it reads a plain
host file the launcher laid down, keyed by the Attempt's own directory. The
filename is shared with the launcher by value, the same way `inspect-attested`
already is -- `host` sits above `adapters`, so neither imports the other.
"""

from __future__ import annotations

from pathlib import Path

from atelier2.contracts.agent_attempts import (
    RunnerGenerationBinding,
    RunnerTerminalEvidenceAckTombstone,
    RunnerTerminalEvidenceEnvelope,
    RunnerTerminalEvidenceHash,
)
from atelier2.contracts.runner_terminal_evidence_codec import (
    MAXIMUM_RUNNER_TERMINAL_EVIDENCE_RECORD_BYTES,
    RunnerTerminalEvidenceRecordCorrupt,
    decode_runner_terminal_evidence_record,
)
from atelier2.ports.agent_attempts import (
    RunnerTerminalEvidenceAcknowledgement,
    RunnerTerminalEvidenceSourceReadback,
)

# The launcher's half of this filename convention lives at
# `atelier2.host.runner_launcher._RETAINED_TERMINAL_RECORD_NAME`; this is the
# read side of the same physical contract, shared by value across the seam.
_RETAINED_TERMINAL_RECORD_NAME = "retained-terminal-record"
_HANDOFF_DIRECTORY_NAME = "handoff"


class FileRunnerTerminalEvidenceSource:
    """The retained terminal record as a file under an Attempt's handoff.

    `attempts_root` is the one directory tree Serve lays each Attempt's
    material under -- the same `lease-root/attempts` the lease publisher writes
    (`atelier2.adapters.file_runner_leases.FileRunnerLeasePublisher`). A record
    is read only for the exact generation asked about, and only ever the
    launcher's bounded bytes; anything else is answered as one of the codec's
    own named non-results, never guessed at.
    """

    def __init__(self, attempts_root: Path) -> None:
        self._attempts_root = attempts_root

    def readback(
        self, binding: RunnerGenerationBinding
    ) -> RunnerTerminalEvidenceSourceReadback:
        record = (
            self._attempts_root
            / binding.attempt_id.value
            / _HANDOFF_DIRECTORY_NAME
            / _RETAINED_TERMINAL_RECORD_NAME
        )
        decoded = decode_runner_terminal_evidence_record(self._retained_bytes(record))
        if (
            isinstance(
                decoded,
                (RunnerTerminalEvidenceEnvelope, RunnerTerminalEvidenceAckTombstone),
            )
            and decoded.binding != binding
        ):
            # A record for another generation is not this Attempt's terminal
            # fact -- treating it as one would converge a lie. It is refused as
            # corrupt for this binding rather than committed.
            return RunnerTerminalEvidenceRecordCorrupt()
        return decoded

    def acknowledge(
        self,
        envelope: RunnerTerminalEvidenceEnvelope,
        accepted_hash: RunnerTerminalEvidenceHash,
    ) -> RunnerTerminalEvidenceAcknowledgement:
        """Acknowledge a committed fact whose Runner is already gone.

        The retained handoff copy is static and one-way, and the Runner that
        wrote it has exited -- there is nothing to notify and nothing to write
        back. Core's own durable acknowledgement is the whole of the ACK here,
        so this yields the tombstone that records it and can never be
        unavailable. The hash is the one Core committed under, recomputed from
        this exact envelope; a mismatch would be a Core-side defect, not a
        state this converges around.
        """
        if RunnerTerminalEvidenceHash.for_envelope(envelope) != accepted_hash:
            raise ValueError("runner evidence ACK hash differs from its envelope")
        return RunnerTerminalEvidenceAckTombstone(
            envelope.binding, envelope.invocation_id, accepted_hash
        )

    def _retained_bytes(self, record: Path) -> bytes | None:
        """The retained record's bytes, or nothing when none was ever laid down.

        A read bounded before a byte is decoded: the file was written by the
        launcher out of the least-trusted process on the host, so a record over
        the canonical bound is refused as oversized by the codec rather than
        pulled whole into Serve.
        """
        try:
            with record.open("rb") as handle:
                payload = handle.read(MAXIMUM_RUNNER_TERMINAL_EVIDENCE_RECORD_BYTES + 1)
        except FileNotFoundError:
            return None
        return payload
