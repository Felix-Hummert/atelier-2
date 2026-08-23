"""Ports Serve composes against once it publishes Runner leases (`#540` C-3.2).

Inert on its own: nothing calls a publisher or an invocation channel yet, and
nothing will until a Core-side driver is wired in (`#540` C-3.4/C-3.6). What
lives here is the seam that driver will need -- the request a lease is
published from, the two outcomes publishing and withdrawing can have, and the
material a launcher hands back once it has actually established the Runner a
lease named.

`atelier2.host.runner_launcher.FileRunnerLeaseSource` is the read side of the
same directory convention (`open`/`claimed`/`released`); a publisher owns the
write side, and the two meet only through the files on disk, never through an
import -- the whole point of the lease being the portability seam (`#540`
C-3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.agent_attempts import RunnerGenerationBinding
from atelier2.contracts.runner_leases import RunnerLeaseId
from atelier2.contracts.runner_manifests import RunnerManifestV1
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable


@dataclass(frozen=True, slots=True)
class RunnerLeaseRequest:
    """One Attempt as Serve is ready to hand a launcher, before its lease exists.

    Everything a launcher will read back out of the published lease
    (`atelier2.contracts.runner_leases.RunnerLease`), except the directories a
    publisher derives and builds itself from `lease_id` -- naming a second
    source for them would only invite the two to disagree. The three
    certificate/document fields are already the exact bytes a launcher's
    handoff copies verbatim into the Runner container: Serve is the one
    holder of the console's own identity and its Runner-facing peer document,
    and this request is where that material first reaches disk.
    """

    lease_id: RunnerLeaseId
    binding: RunnerGenerationBinding
    manifest: RunnerManifestV1
    runner_image: str
    serve_container: str
    ca_certificate: bytes
    core_certificate: bytes
    core_peer_document: bytes


@dataclass(frozen=True)
class RunnerLeasePublished:
    """This call wrote the lease and every document it names."""

    lease_id: RunnerLeaseId


@dataclass(frozen=True)
class RunnerLeaseExisting:
    """A lease of this id already stands, publishing exactly this request again.

    Serve retries a durable write it cannot itself confirm landed; a second
    call with the same content is that retry succeeding quietly rather than
    building a second Attempt for the id the first one already claimed.
    """

    lease_id: RunnerLeaseId


type PublishRunnerLeaseResult = (
    RunnerLeasePublished
    | RunnerLeaseExisting
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class RunnerLeaseWithdrawn:
    """Withdraw won: no launcher had claimed this lease, and now none can.

    Every directory this publisher built for the Attempt is gone with it -- a
    launcher that later found this lease id on disk would find no material to
    act on either.
    """

    lease_id: RunnerLeaseId


@dataclass(frozen=True)
class RunnerLeaseAlreadyClaimed:
    """Withdraw lost: a launcher claimed this lease first.

    The Attempt may still be running or may already be finished; either way
    it was never withdrawn, and a caller deciding whether an Attempt ever ran
    reads this apart from `RunnerLeaseWithdrawn` for exactly that reason
    (`#540` Kind #584 (#X)). Once the lease reaches `released`, its material
    is removed the same as a won withdraw's -- only the answer about whether
    the Runner ever ran stays apart.
    """

    lease_id: RunnerLeaseId


type WithdrawRunnerLeaseResult = RunnerLeaseWithdrawn | RunnerLeaseAlreadyClaimed


class RunnerLeasePublisher(Protocol):
    """Serve's write side of the lease directory a launcher claims from."""

    def publish(self, request: RunnerLeaseRequest) -> PublishRunnerLeaseResult: ...

    def withdraw(self, lease_id: RunnerLeaseId) -> WithdrawRunnerLeaseResult: ...


@dataclass(frozen=True)
class RunnerPeerMaterial:
    """What a launcher hands back once this Attempt's Runner is established:
    the client leaf it issued this one invocation (`peer/client.crt`) and its
    own attestation that the container it started matches the manifest Core
    bound (`handoff/inspect-attested`) -- the two documents only a launcher
    ever writes, and what a Core-side session driver (`#540` C-3.4) needs
    before it may accept this invocation's one TLS connection.
    """

    client_certificate: bytes
    inspect_attestation: str


@dataclass(frozen=True)
class RunnerInvocationTimedOut:
    """A launcher never produced this Attempt's peer material in time.

    Named apart from a raised exception so a caller serving many Attempts
    answers for the one that never came up without stopping the rest -- the
    same standard every other Runner-facing boundary in this codebase reads
    under.
    """

    lease_id: RunnerLeaseId


type RunnerInvocationOutcome = RunnerPeerMaterial | RunnerInvocationTimedOut


class RunnerInvocationChannel(Protocol):
    """Where Serve reads what a launcher only ever produces once, per Attempt."""

    def serve_one_invocation(
        self, lease_id: RunnerLeaseId, deadline_seconds: float
    ) -> RunnerInvocationOutcome: ...
