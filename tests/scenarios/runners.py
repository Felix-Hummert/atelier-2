from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from atelier2.contracts.agent_attempts import (
    RunnerBindingConflict,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerTerminalEvidenceAckTombstone,
    RunnerTerminalEvidenceEnvelope,
    RunnerTerminalEvidenceHash,
    RunnerTerminalEvidenceReadback,
)
from atelier2.contracts.runner_terminal_evidence_codec import (
    RunnerTerminalEvidenceRecordMissing,
    decode_runner_terminal_evidence_record,
    encode_runner_terminal_evidence_record,
)
from atelier2.ports.agent_attempts import (
    RunnerTerminalEvidenceAcknowledgement,
    RunnerTerminalEvidenceAcknowledgementUnavailable,
    RunnerTerminalEvidenceSourceReadback,
)


class SimulatedRunnerCrash(RuntimeError):
    pass


@dataclass
class _AcceptedGeneration:
    binding: RunnerGenerationBinding
    invocation_id: RunnerInvocationId
    launched: bool = False
    record: bytes | None = None
    acknowledgement_calls: int = 0
    garbage_collection_count: int = 0
    fail_before_readback: bool = False
    fail_after_acknowledge: bool = False
    fail_acknowledgement: bool = False
    acknowledge_entered: threading.Event | None = None
    acknowledge_release: threading.Event | None = None
    readback_probe: Callable[[], None] | None = None
    acknowledgement_probe: Callable[[], None] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class FakeRunner:
    """One in-memory Runner journal, synchronized independently per generation."""

    def __init__(self) -> None:
        self._accepted: dict[RunnerGenerationId, _AcceptedGeneration] = {}
        self._registry_lock = threading.Lock()

    def accept(self, binding: RunnerGenerationBinding) -> RunnerInvocationId:
        with self._registry_lock:
            accepted = self._accepted.get(binding.generation_id)
            if accepted is not None:
                self._require_binding(accepted, binding)
                return accepted.invocation_id
            invocation_id = RunnerInvocationId(
                f"runner-invocation-{binding.generation_id.value}"
            )
            self._accepted[binding.generation_id] = _AcceptedGeneration(
                binding, invocation_id
            )
            return invocation_id

    def launch(
        self,
        binding: RunnerGenerationBinding,
        invocation_id: RunnerInvocationId,
    ) -> None:
        accepted = self._accepted[binding.generation_id]
        with accepted.lock:
            self._require_binding(accepted, binding)
            if accepted.invocation_id != invocation_id:
                raise RunnerBindingConflict(
                    "runner launch differs from accepted generation"
                )
            accepted.launched = True

    def observe(
        self, envelope: RunnerTerminalEvidenceEnvelope
    ) -> RunnerTerminalEvidenceEnvelope:
        accepted = self._accepted[envelope.binding.generation_id]
        with accepted.lock:
            if accepted.binding != envelope.binding:
                raise RunnerBindingConflict(
                    "runner evidence differs from accepted binding"
                )
            if envelope.invocation_id not in (None, accepted.invocation_id):
                raise RunnerBindingConflict("runner evidence differs from invocation")
            decoded = decode_runner_terminal_evidence_record(accepted.record)
            if isinstance(decoded, RunnerTerminalEvidenceAckTombstone):
                raise RunnerBindingConflict("runner evidence was already acknowledged")
            if isinstance(decoded, RunnerTerminalEvidenceEnvelope):
                return decoded
            if not isinstance(decoded, RunnerTerminalEvidenceRecordMissing):
                raise RunnerBindingConflict("runner evidence record is unusable")
            accepted.record = encode_runner_terminal_evidence_record(envelope)
            return envelope

    def readback(
        self, binding: RunnerGenerationBinding
    ) -> RunnerTerminalEvidenceSourceReadback:
        accepted = self._accepted[binding.generation_id]
        with accepted.lock:
            self._require_binding(accepted, binding)
            if accepted.fail_before_readback:
                accepted.fail_before_readback = False
                raise SimulatedRunnerCrash("before terminal-evidence readback")
            if accepted.readback_probe is not None:
                accepted.readback_probe()
            readback = decode_runner_terminal_evidence_record(accepted.record)
            if isinstance(
                readback,
                (RunnerTerminalEvidenceEnvelope, RunnerTerminalEvidenceAckTombstone),
            ):
                self._require_readback_binding(readback, binding)
            return readback

    def acknowledge(
        self,
        envelope: RunnerTerminalEvidenceEnvelope,
        accepted_hash: RunnerTerminalEvidenceHash,
    ) -> RunnerTerminalEvidenceAcknowledgement:
        accepted = self._accepted[envelope.binding.generation_id]
        with accepted.lock:
            self._require_binding(accepted, envelope.binding)
            accepted.acknowledgement_calls += 1
            if accepted.fail_acknowledgement:
                accepted.fail_acknowledgement = False
                return RunnerTerminalEvidenceAcknowledgementUnavailable()
            if accepted.acknowledgement_probe is not None:
                accepted.acknowledgement_probe()
            evidence_hash = RunnerTerminalEvidenceHash.for_envelope(envelope)
            if accepted_hash != evidence_hash:
                raise RunnerBindingConflict("runner ACK hash differs from evidence")
            expected = RunnerTerminalEvidenceAckTombstone(
                envelope.binding, envelope.invocation_id, accepted_hash
            )
            retained = decode_runner_terminal_evidence_record(accepted.record)
            if isinstance(retained, RunnerTerminalEvidenceAckTombstone):
                if retained != expected:
                    raise RunnerBindingConflict("runner ACK differs from tombstone")
            elif isinstance(retained, RunnerTerminalEvidenceEnvelope):
                if retained != envelope:
                    raise RunnerBindingConflict("runner ACK differs from evidence")
                accepted.record = encode_runner_terminal_evidence_record(expected)
                accepted.garbage_collection_count += 1
            else:
                raise RunnerBindingConflict("runner ACK has no usable evidence")

            if accepted.acknowledge_entered is not None:
                accepted.acknowledge_entered.set()
                release = accepted.acknowledge_release
                if release is None or not release.wait(timeout=5):
                    raise AssertionError("scenario Runner ACK barrier was not released")
            if accepted.fail_after_acknowledge:
                accepted.fail_after_acknowledge = False
                raise SimulatedRunnerCrash("after terminal-evidence ACK and GC")
            return expected

    def observed_evidence(
        self, binding: RunnerGenerationBinding
    ) -> RunnerTerminalEvidenceEnvelope | None:
        accepted = self._accepted[binding.generation_id]
        with accepted.lock:
            self._require_binding(accepted, binding)
            readback = decode_runner_terminal_evidence_record(accepted.record)
            return (
                readback
                if isinstance(readback, RunnerTerminalEvidenceEnvelope)
                else None
            )

    def retain_record(
        self, binding: RunnerGenerationBinding, record: bytes | None
    ) -> None:
        accepted = self._accepted[binding.generation_id]
        with accepted.lock:
            self._require_binding(accepted, binding)
            accepted.record = record

    def record_bytes(self, binding: RunnerGenerationBinding) -> bytes | None:
        accepted = self._accepted[binding.generation_id]
        with accepted.lock:
            self._require_binding(accepted, binding)
            return accepted.record

    def fail_next_readback(self, binding: RunnerGenerationBinding) -> None:
        self._accepted[binding.generation_id].fail_before_readback = True

    def fail_after_next_acknowledge(self, binding: RunnerGenerationBinding) -> None:
        self._accepted[binding.generation_id].fail_after_acknowledge = True

    def fail_next_acknowledgement(self, binding: RunnerGenerationBinding) -> None:
        self._accepted[binding.generation_id].fail_acknowledgement = True

    def hold_acknowledge(
        self,
        binding: RunnerGenerationBinding,
        entered: threading.Event,
        release: threading.Event,
    ) -> None:
        accepted = self._accepted[binding.generation_id]
        accepted.acknowledge_entered = entered
        accepted.acknowledge_release = release

    def probe_calls(
        self,
        binding: RunnerGenerationBinding,
        readback: Callable[[], None],
        acknowledge: Callable[[], None],
    ) -> None:
        accepted = self._accepted[binding.generation_id]
        accepted.readback_probe = readback
        accepted.acknowledgement_probe = acknowledge

    def acknowledgement_count(self, binding: RunnerGenerationBinding) -> int:
        return self._accepted[binding.generation_id].acknowledgement_calls

    def garbage_collection_count(self, binding: RunnerGenerationBinding) -> int:
        return self._accepted[binding.generation_id].garbage_collection_count

    @property
    def accepted_generation_ids(self) -> frozenset[RunnerGenerationId]:
        return frozenset(self._accepted)

    @property
    def provider_start_count(self) -> int:
        return sum(accepted.launched for accepted in self._accepted.values())

    @staticmethod
    def _require_binding(
        accepted: _AcceptedGeneration, binding: RunnerGenerationBinding
    ) -> None:
        if accepted.binding != binding:
            raise RunnerBindingConflict("runner generation is bound to different work")

    @staticmethod
    def _require_readback_binding(
        readback: RunnerTerminalEvidenceReadback,
        binding: RunnerGenerationBinding,
    ) -> None:
        if readback.binding != binding:
            raise RunnerBindingConflict(
                "runner readback differs from requested binding"
            )
