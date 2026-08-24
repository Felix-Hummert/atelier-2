# Operations

Audience: the human operator deciding how this installation is started,
stopped, redeployed, how an executor toolchain is pinned, or how an older
store is raised to the current schema.

This file owns that runbook. It does not own product intent, requirement
sentences, or trust-boundary decisions. [PRODUCT.md](PRODUCT.md) says what
exists; [ADR 0009](decisions/0009-runner-trust.md) owns network hardening and
reachability; [ADR 0001](decisions/0001-durable-runtime.md) owns the schema
versions and fingerprints. This file only says how the packaged process is
started, how a predecessor store is raised offline, and how a deployment pins
an executor toolchain.

No operations owner existed. [docs/README.md](README.md) now names this file
for that question. [Journeys](journeys/) illustrate requirements and bind
nothing.

## Disposable Serve candidate

The repository `Dockerfile` bakes the locked project and production cockpit
into one provider-free Serve image. It has no provider executable,
credential/configuration mount, host home, scratch mount, Docker socket,
system service access, added capability, privileged mode, or Runner service.
It runs non-root with a read-only root filesystem, dropped capabilities and
`no-new-privileges`. Its only writable durable state is one Compose-named
`store` volume.

Start it from a clean checkout at a committed tree:

```bash
bash scripts/container_up.sh
```

The script refuses dirty, untracked, or unreadable source before Docker, then
archives the resolved commit into a temporary build context. It binds that
commit and tree to image and Compose resource labels, creates a fresh Compose
project, waits for `/atelier/api/v1/health` to report that same identity, and
only then prints the Docker-assigned loopback URL. A private candidate-lifecycle
descriptor freezes its teardown shape, so the shell-quoted
`down --volumes --rmi local --remove-orphans` command carries the exact identity
without ambient variables or a mutable checkout. It removes only this
candidate's container, network, volume and local project image; errors and
interrupts attempt that same cleanup and preserve the descriptor for retry when
it fails. Successful teardown removes it. Other containers and services are not
selected or changed. A rerun creates a new disposable candidate. The current Core
`ExactOutput` executor can still serve its fixture; this package supplies no
external provider or Runner. The entrypoint can carry an operator-declared
Runner-lease deployment from `ATELIER2_RUNNER_*` environment variables ("Serve
as the lease writer", below); this candidate declares none of them and starts
runner-free.

## Disposable #301-A Runner candidate

This is not the stable Serve installation and must not be pointed at it. It
does not mutate A.1 compose, its network, or its store. From this checkout, on
a local rootful Docker engine the operator has authorized:

```bash
bash scripts/runner_candidate.sh success
bash scripts/runner_candidate.sh cancel
bash scripts/runner_candidate.sh resume
bash scripts/runner_candidate.sh toolchain
bash scripts/runner_candidate.sh egress
bash scripts/runner_candidate.sh console
```

`success`, `cancel` and `resume` drive one full session each. `toolchain`,
`egress` and `console` drive no session at all: they measure the deployed
image, the Attempt network form and the long-lived console's own policy,
unbilled, with no credential and no provider call.

A session scenario is not its own launcher. The script starts one disposable
Core witness — the console's stand-in, started on its own base network exactly
as compose starts the real console — writes one lease document, and hands the
rest to the real `atelier2-runner-launcher` described below. Everything a
launcher does is therefore proven here by the same code an installation runs,
against a Core this launcher did not start. Every engine operation the script
itself still performs goes through the launcher's own typed owner,
`atelier2.adapters.docker_carrier`, in argument vectors rather than shell
strings. What it calls the engine for directly is deliberately not a launcher
operation: the image builds, the probe legs of `toolchain`, `egress` and
`console`, putting a Core witness on its base network the way compose does, and
`clean`'s own directory maintenance — all of which measure, deploy or tidy from
the outside.

Each session scenario ends with one labelled Attempt network, one base network,
one disposable Core witness, one Runner, one handoff tmpfs volume, and one
identity and one journal volume. The launcher removes every object it created
once the Runner answers `RELEASED`; the base network and the Core witness are
the witness's own, standing in for the deployment's. On failure they stay,
named, plus the witness directory under
`/var/tmp/atelier2-301a-runner-witness.*`. The witness mints the console
identity through the production command (`issue-console-identity` below). The
launcher unlinks the client key as soon as the identity receiver has taken it,
and the witness unlinks its own disposable authority and Core keys afterwards,
through held directory FDs, keeping only public certificate metadata in that
tree. Public bootstrap reaches the Runner by container copy; Core reads the
launcher's inspect attestation from a read-only path. Core listens as
`core.runner-candidate.internal:8443` on that Attempt network only. The
witness's hook into the host owners is
`tests/witness/runner_candidate_issuer.py`, a command line for the manifest, the
identity-record read and the key unlink; it is never copied into an image.

The identity and journal volumes are durable local volumes, not tmpfs: `resume`
proves a real candidate process death (declared `CandidateScenario`
`CRASH_AFTER_PUBLISH`, right after the terminal fact is journaled but before
Core is told) survives this exact Runner container's own restart, and only a
volume whose content outlives that restart can carry the journal's retained
fact or the mTLS identity Core has already armed forward. A tmpfs-backed
volume cannot — its content disappears once no container has it mounted, which
a stopped container's own restart always crosses. This is a deliberate,
disposable-witness-only tradeoff: the Runner's client private key now touches
real disk under Docker's volume data root for the witness's lifetime, removed
by the same teardown as every other labelled object once released. Handoff
stays tmpfs; its content is fully reproducible from files the launcher already
retains on the host, so `resume` simply re-copies them into the restarted
container instead of needing them to survive on their own.

**The Runner's writable surface.** The image root stays read-only and carries
the whole toolchain: node, the Claude CLI pinned to a release out of
`CONFORMANT_CLAUDE_VERSIONS`, and bubblewrap. Exactly one path is writable —
`/tmp` — and it is a `noexec,nosuid` tmpfs of an attested size, so the provider
child may write data there and may never execute it or gain privilege from it.
The provider's credential directory is **read-only and not executable**: ADR
0009 §2's 2026-08-22 amendment admits exactly that one host bind beyond the
per-invocation identity material, because a live operator session may hold that
directory open, and a write-capable per-Attempt copy waits on its own operator
ruling. Being a bind mount, it is the one surface the launcher cannot mount
`noexec`, so the *grant* forbids execution instead — a real credential
directory carries plugins, hooks and shell snippets the child must read and
must never run, and putting that in the right rather than in a convention makes
it part of the manifest identity Core selected. Execution is granted only where
the image root's own code lives. Which paths a
child may touch, and with which right, is a manifest fact: `RunnerManifestV1`
carries the whole allowlist, the Runner installs exactly that as its Landlock
ruleset, and the launcher's inspect attestation re-reads the mount flags and
size of the writable entry, requires the credential directory to be bound
read-only, and refuses any other host bind at all. Widening the surface
therefore changes the manifest identity Core selected and refuses, rather than
passing unnoticed. Anything outside the allowlist is denied by Landlock; a
read-only entry denies writes even where the mount would allow them; and an
attested path this image does not have refuses before the child starts.

**The Attempt network.** Each Attempt gets its own routed bridge network, not
an internal one. Every container the launcher creates is created attached to
*no* network; a throwaway `CAP_NET_ADMIN` container installs that Attempt's
chains inside its network namespace and exits, and only then is it connected —
so an Attempt never reaches a container before its own filter does, and a
policy that fails to install leaves a container unable to reach that Attempt
rather than running wide open. `resume` has the same order rather than an
exception to it: the container it restarts is released from every network
first, read back as reachable by nothing, and only then policed and reconnected.
The Runner itself carries no packet-filtering tool and no
capability to alter what was left. The Runner may reach outbound DNS,
outbound HTTPS, and its own Attempt subnet for Core. Core may reach nothing
outbound beyond its Attempt subnets and the base network it serves the cockpit
on: it holds the private key and the only store of product truth and has no
business on the Internet — which means a policed console loses whatever
Internet reach its base network gave it, deliberately. Inbound, exactly one
opening exists per Attempt: Core accepts its own session port from that
Attempt's subnet, because Core is the only container that serves anything. The
Runner accepts nothing inbound at all — it dials out, and its answers return as
established connections. Everything else, in either direction, is REJECTed —
including IPv6, which gets a blanket reject chain no Attempt widens, because
Attempt networks are IPv4 — so a forbidden connection fails immediately with
`Connection refused` and the provider CLI's own error handling surfaces it,
rather than a silent DROP the operator would have to diagnose by timeout.

**One chain per Attempt, in a console that outlives them.** A container's own
`INPUT`/`OUTPUT` chains carry the base policy once — loopback, established
answers, the Runner's DNS and HTTPS or the console's base network, a jump into
the Attempt dispatch chain, and then the rejects. Each Attempt then owns a
named pair of chains reached from that dispatch chain, and releasing an Attempt
removes them whole and reads the namespace back to prove they are gone — a
listing that could not be taken at all is a refusal of its own, never a clean
release. So the console can be attached to one Attempt after another without
rules accumulating, and a stopped console has no namespace and therefore no
residue. What says a namespace already carries the base policy is an empty
`ATELIER2-BASE-INSTALLED` chain the base policy writes *last*: an install that
died halfway leaves no sentinel, so the next Attempt installs the whole base
policy again instead of attaching to a namespace whose default-deny was never
written. `console` measures all of it: the console keeps answering on
its own port from the host and from its base network while an Attempt runs, an
Attempt network reaches the console on the session port and is refused
immediately on the cockpit port, two Attempts run one after the other against
the same console, and after each release the namespace names no rule of it.

Cross-Attempt unreachability is proven rather than assumed: a second Attempt
probing the first is refused by its *own* outbound policy, because the other
Attempt's address is neither in its subnet nor on an allowed port. That is
loud and immediate, where the host's inter-network isolation alone would only
drop the packet and make the operator wait out a timeout. `egress` measures all
of it — a real name resolves, HTTPS connects, ports 80 and 25, every inbound
attempt and every cross-Attempt attempt refuse in under a second, and the
namespace is asserted to carry no global IPv6 path. The probed Attempt really
listens on the ports being probed, proven first over its own loopback, so the
refusals measure the policy rather than an absent service.

**What `toolchain` measures.** `claude --version` in the hardened container
must report the pinned release; the runner-side pre-start attestation must
answer typed for all three cases — the fake-free executor measures *no* CLI
(a declared absence, not a skipped check), the Claude executor measures its
version and then refuses because no personal-subscription credential is
present, and a manifest naming an executor revision this image pins no
toolchain for refuses before any provider start. The leg also records whether
bubblewrap can start a namespace under the session hardening and asserts
nothing about the answer: on a host whose Docker default seccomp profile
denies user-namespace creation the exit is 1, and that is a measurement for
the owning item to rule on, never a reason to soften the container.

```bash
bash scripts/runner_candidate.sh clean
bash scripts/runner_candidate.sh images
```

`clean` removes only witness directories whose run reached `RELEASED`. It
records that run's Attempt network only once the launcher reported creating
it, so a recorded network's absence is the release proof; a directory
with no recorded network — including one whose witness is still mid-run — or
whose recorded network still exists, is left untouched. Failure-analysis
residue is never removed by `clean`. Clearing a released directory's
root-owned `core-store` needs the `atelier2-301a-core` image; `clean` refuses
with a named reason if that image is missing rather than pulling one.
`images` removes only the candidate images that are actually present, so
running it again after `clean` or a prune is a no-op, not a failure; it is
never run implicitly by `success` or `cancel`, so a normal run keeps reusing
Docker's build cache.

## The Runner launcher

`atelier2-runner-launcher` is the only process on this host that talks to
Docker. It runs *beside* the console, never inside it: Serve receives no engine
socket, no privileged broker, and no carrier call of any kind (ADR 0009 §2, the
2026-08-23 operator ruling on `#540`).

Mint the identity the console serves Attempt sessions under first, out of this
installation's own authority. The console reads that directory read-only and
copies its `ca.crt` and `core.crt` into each Attempt's handoff:

```bash
uv run atelier2-runner-launcher issue-console-identity \
  --certificate-authority-state <directory> \
  --identity <directory>
```

Running it again is the renewal: the authority stands for about a year, the
console's leaf for about a quarter, and the leaf a Runner presents is minted
per invocation for the attempt span its manifest declares. Renew before the
quarter is out — `serve` refuses to start on an expired console identity by
name rather than letting every Attempt fail as an unreadable handshake.

Then start the watcher with the lease directory the console publishes into,
what the console *is* on this host, and what any Attempt may ask this host for:

```bash
uv run atelier2-runner-launcher serve \
  --lease-directory <directory> \
  --certificate-authority-state <directory> \
  --network-policy-image <image> \
  --attempt-root <directory> \
  --console-container <container> \
  --console-network <network> \
  --console-identity <directory> \
  --runner-image <image>
```

**What it may do.** For each lease it claims — exclusively, by moving the lease
document — it creates that Attempt's network, installs that Attempt's chains in
the console's own container and attaches it, creates the Attempt's identity,
handoff and journal volumes, starts the Runner container the attested manifest
describes, hands it the public bootstrap, mints the identity that one
invocation may present, delivers it through a receiver container, writes the
inspect attestation Core admits the session on, waits for the session, and
removes every object it created together with the console's chains for it. All
objects carry `atelier2.runner-lease=<lease>`. `--once` establishes a single
lease and stops; without it the launcher keeps watching at `--poll-seconds`.

**A lease is a request, not an authorization.** It names host directories this
privileged process will mount, the image it will run as root over that
Attempt's volumes, and the container it will attach to an Attempt network, so
it is validated against what *you* declared at start, never against what the
document claims: every path a lease names must resolve inside `--attempt-root`,
its console container must be `--console-container`, and its Runner image must
be `--runner-image`. Its own file name must be a lease id — the same 64-hex
form as the Attempt it names — because every container, volume, label and chain
name of the Attempt is built from it. Anything else is refused by name before
it is read, and what gets mounted is the resolved path this checked, not the
one the document spelled. That is what keeps the seam safe when Serve becomes
the writer of leases (ADR 0009 §2, 2026-08-23 amendments on ruling B) — Serve
may ask for an Attempt and may never command one.

**The manifest must be the one Core bound, and must fit this host.** A lease
carries the manifest document and the manifest identity Core selected
separately; the launcher reads them against each other and refuses an Attempt
whose document is not the one that identity names. What that document then asks
this host for is bounded by what you declared, defaulting to the candidate
Attempt's own numbers:

```bash
  --maximum-memory-bytes <bytes> \
  --maximum-process-limit <count> \
  --maximum-cpu-quota-microseconds <microseconds> \
  --maximum-scratch-bytes <bytes> \
  --maximum-writable-surface-bytes <bytes> \
  --maximum-journal-bytes <bytes>
```

`--maximum-writable-surface-bytes` is the sum of the Attempt's writable grants,
each of which becomes a tmpfs and therefore host memory — bounding one grant
while their count is free would bound nothing. `--maximum-journal-bytes` is the
one number the engine never sees: the journal has to be a durable volume
because the Runner's own restart must find it (`resume`), the local volume
driver gives a disk-backed volume no size, and the Runner keeps that capacity
itself against the manifest it was handed. Bounding the number is what keeps a
lease from deciding how much of this host's disk that promise covers. A
manifest over any of these is refused before the first engine call, so a lease
this host will not carry costs no object at all. These are host protection, not
Attempt correctness: a compromised Serve still chooses its own Attempts'
numbers *within* them (ADR 0009 §2, 2026-08-23 amendment).

**The attempt root holds per-Attempt material and nothing else.** Everything
under it is a surface some lease may ask to have mounted into an Attempt, so
`--certificate-authority-state` must lie outside it; overlapping trees are
refused at start rather than discovered by the lease that used them. Keep the
authority beside the attempt root, never inside it.

**What it may not do.** It never reads or writes the product's own store, never
runs provider code, never publishes a port or takes the host's own network
namespace for a Runner — its inspect attestation refuses a Runner container
that was not created reachable by nothing, and its attachment attestation
refuses any container reaching further than the declared base network and one
Attempt network — and never removes an object that does not carry the lease
label of an Attempt it owns. The authority key stays in
`--certificate-authority-state` on this host: it is never mounted, copied, or
passed into any container, and the client key minted for an invocation is
unlinked the moment its delivery is over, taken or not.

**Failure shape.** A refused engine operation, a container that does not match
the manifest Core bound, a Runner whose own offer or identity cannot be made
into a certificate, or a Runner that exits nonzero with nothing retained in
its journal ends *that Attempt* loudly with a named refusal and leaves its
objects on the host to be read; it is reported as `attempt-failed=…`. A lease
this launcher will not accept at all — one naming something outside what you
declared, a document that cannot be read, or one larger than a launcher will
read — is refused where it is claimed and reported as `lease-refused=…`; it
stays quarantined under the lease directory's `claimed`, never retried. Neither
ends the launcher: the next lease is served, because one bad Attempt is a bad
Attempt and not an outage, and from C-3 one document Serve got wrong must cost
exactly one Attempt. A bounded `--once` run exits nonzero when its lease failed.

**What a failed Attempt gives back.** The console is not left in it. Before the
failure is reported, this Attempt's chains are taken out of the console's
namespace and the console is detached from the Attempt network; a removal that
will not complete is reported as `console-still-holds-the-attempt=…` beside the
Attempt's own refusal. Without that, the next Attempt would be refused by its
own attachment attestation — a console on two Attempt networks — and so would
every Attempt after it, while an ACCEPT rule kept pointing at a subnet the
engine is free to hand out again. Everything the Attempt itself created stays
where it is, named, for you to read and for the next start to reconcile.

The one restart the launcher performs is the opposite case: a Runner that exits
nonzero but did retain a terminal record still holds the only account of what
happened. That exact container is released from its Attempt network first,
started again, and only then policed and reconnected — the same order a first
start has, because a restart throws away the namespace the policy lived in.

Residue is reconciled, not swept: at start the launcher releases every lease
still marked claimed by a launcher that is gone and removes exactly the objects
carrying those leases' labels, including the console's chains for them. An
Attempt that may already have run is never silently run a second time — what
its owner does with an interrupted lease is that owner's decision. A claimed
document whose name is not a lease id never named an object at all and is only
released; residue nobody can clear — a console that is already gone — is
reported as `reconcile-refused=…` and stepped over, because refusing to start
over a leftover would turn it into an outage.

**Exactly one launcher per lease directory.** Reconciliation identifies an
abandoned Attempt by its lease sitting in `claimed`, which is also what a
working launcher's live Attempt looks like — a second launcher beside a first
would tear a running Attempt down. A launcher therefore takes an exclusive
`flock` on `<lease-directory>/.launcher.lock` before it reconciles anything and
refuses by name when another holds it. The claim is the kernel's: it disappears
with the process, including one that was `SIGKILL`ed, so a crashed launcher
leaves nothing to clear by hand. Its bound is this host — a launcher fleet
across hosts needs the ownership token `#540` C-2 named, which a lock the other
host cannot see is not.

**What this form still leaves standing.** Each of these is measured, bounded,
and named here rather than left for an operator to discover.

- **A lease's paths are admitted, not held.** A resolved path is a string, and
  the engine resolves it again when it binds it. Serve owns the attempt root,
  so a compromised Serve can swap a directory component between the check and
  the mount and have a host directory read into its own Attempt. That is inside
  the boundary ADR 0009 §2's amendment (a) draws — the host stays protected,
  the Attempt does not — and closing it needs the launcher to *build* the
  attempt root rather than validate one Serve built (`#540` C-3.2/C-3.6).
- **An Attempt's chain name carries a bounded prefix of its id.** Two Attempts
  whose prefixes met are refused at chain creation; if one were released while
  the other ran, the release would flush the chain they share. That closes a
  live Attempt's grants rather than opening anything.
- **A policed console has no IPv6 at all**, inbound or outbound: Attempt
  networks are IPv4, and the base policy rejects the other family whole.
- **A Runner's leaf covers its manifest's attempt span plus clock skew.** An
  Attempt that crashed near the end of its span and is resumed can come back to
  an expired leaf; the session then fails closed at the handshake.
- **Renewal replaces a key and a certificate as two files.** Each is renamed
  into place whole, so neither is ever half-written, but a console reading
  between the two renames holds a key that does not match the certificate
  beside it. Renew, then restart the console.
- **The console loses its base network's Internet reach the moment its first
  Attempt is attached**, deliberately (ADR 0009 §2). A deployment whose Serve
  needs outbound reach must settle that before the live cutover (`#540` C-3.6).
- **A Runner image that ignores its own manifest can still fill this host's
  disk.** The journal volume is durable and the local driver gives it no size,
  so the capacity is kept by the Runner against its manifest and bounded by
  `--maximum-journal-bytes` above. That closes the lease-chosen half; the image
  itself is what you declared with `--runner-image`.

## Serve as the lease writer (`#540` C-3.6)

Serve itself can be composed as the process that *writes* the leases the
launcher above claims — the fake-free-only slice of the live cutover, over
`atelier2.adapters.file_runner_leases.FileRunnerLeasePublisher`; Serve never
touches Docker. Its Runner-lease deployment is one group of six
`atelier2 serve` flags — `--runner-lease-root`, `--runner-image`,
`--runner-image-digest`, `--runner-console-container`,
`--runner-core-identity-directory` (a directory holding the console's
`ca.crt`/`core.crt`/`core.key`) and `--runner-accept-timeout-seconds` —
declared together or refused by name at start; the manifest's source commit is
the already-required `--source-commit`. The packaged container entrypoint
carries each flag from the environment variable of the matching name
(`ATELIER2_RUNNER_LEASE_ROOT`, `ATELIER2_RUNNER_IMAGE`,
`ATELIER2_RUNNER_IMAGE_DIGEST`, `ATELIER2_RUNNER_CONSOLE_CONTAINER`,
`ATELIER2_RUNNER_CORE_IDENTITY_DIRECTORY`,
`ATELIER2_RUNNER_ACCEPT_TIMEOUT_SECONDS`) and validates none of them itself —
the serve boundary owns the all-or-nothing refusal. The lease root must be
bind-mounted into the container at the same absolute path the launcher was
given, and the core-identity directory arrives as a run-time mount — mount it
read-only; the image bakes no identity file, key, or runner value. A
container started without these variables serves exactly as before,
runner-free. Only the fixed
fake-free candidate is served this way; a real provider over a Runner lease
waits on `#15` and B-3.

**Every start withdraws its own open leases first**, before anything else
touches the lease directory: a lease this exact process published and never
saw claimed before its own restart would otherwise sit `open` until some
launcher happens to poll past it — and a launcher that does, hours later,
would start a Runner container for an Attempt whose driving workflow this
process no longer owns. Withdrawal is one-way here: it moves the lease to
`withdrawn/` and deletes the attempt material, so a recovered workflow that
republishes is answered `RunnerLeaseExisting` and no fresh open lease
reappears. Such an Attempt fails fast rather than polling its own deleted
attempt paths for the full accept deadline, and is converged to its real
terminal at the next start (below). A lease a launcher already claimed loses
this race harmlessly and is left for its launcher.

**Every start converges every Runner-lease Attempt no workflow still owes its
next move.** After a Serve restart mid-session such an Attempt would otherwise
stand `LAUNCH_ARMED`/`POSSIBLY_RAN`, its run `STARTED`, forever. The launcher
lays that Attempt's own retained terminal record in its handoff directory
before the Attempt is ever removed; Serve reads it back and commits it to the
terminal the Runner actually reported — exactly once, and never the invented
`INTERRUPTED` the driverless sweep would write (`runner_lease_attempt_converged`
per Attempt, with a `runner_lease_convergence_total`). An Attempt whose fact
never reached the handoff — none was retained, or the record is unreadable — is
left exactly as durable as it was and named `runner_lease_attempt_left_nonterminal`
for you to read, never forced to a terminal it cannot prove.

**At most one Runner Attempt runs at a time.** The Core session listener binds
one fixed port (`atelier2.adapters.runner_tls.CORE_SESSION_PORT`) per Serve
process, so a second, concurrent Runner-lease Attempt waits for the first to
release rather than failing — logged as `agent_attempt_awaiting_runner_slot`.
No run ends unsuccessfully only because another Runner Attempt was already
running.

**One named gap, until its own item lands.** An Attempt that crashed between
binding its Runner generation and publishing its lease is manifest-bound with
no lease document at all; a later cancel finds nothing to withdraw and fails
loud with `RunnerLeaseUnknown`, leaving the Attempt `CANCEL_REQUESTED` rather
than lying about a terminal it cannot prove. Its durable close belongs to the
never-launched cancel path (Kind #584, `#540`); the convergence above resolves
a launched Attempt's terminal, not one that never got a lease. Restarting or
updating this deployment mid-session no longer strands a live Runner-lease
Attempt: the convergence above brings it to the terminal its Runner reported on
the next start.

## Stable local Serve installation

From a clean committed checkout, install the one stable provider-free console:

```bash
bash scripts/container_live.sh install
```

The command refuses an active or enabled host Atelier service, a listener on
port 8422, another Docker resource labelled as the stable deployment, ambient
Compose mode values, or an existing accepted installation. It archives the
committed source through the same snapshot owner as the disposable candidate,
records durable `INSTALLING` intent before Docker can mutate anything, and
publishes the completed exact container, image, volume, network, configuration,
engine, source and frozen-descriptor identity only after health succeeds. The
private record lives under
`${XDG_STATE_HOME:-$HOME/.local/state}/atelier2/container-live`.

The console then owns `http://127.0.0.1:8422/atelier/`, uses
`restart: unless-stopped`, and preserves its Compose volume. Operate only its
recorded identity:

```bash
bash scripts/container_live.sh status
bash scripts/container_live.sh stop
bash scripts/container_live.sh start
bash scripts/container_live.sh uninstall
bash scripts/container_live.sh update
```

`status` is read-only and prints exactly `RUNNING`, `STOPPED`, `INCOMPLETE`, or
`DRIFTED`. `stop` and `start` first validate the complete record and then address
only its exact container ID; they never rebuild, recreate, search for a
replacement, or adopt a listener or Docker resource. A failed start stops that
same container and leaves the volume intact. A failed install removes only its
intent-owned project when exact identity can be proved; otherwise it leaves the
incomplete record and fails loudly.

`uninstall` tears the installation down completely — container, network,
volume, image, and the record directory itself — reading only its own
record and compose truth, never an operator-supplied variable. It is
idempotent: nothing installed is a clean success. When the record is exact it
tears down through `docker compose down --volumes --rmi local
--remove-orphans`; when the record is missing, corrupt, or its exactness
cannot be proved (the "record gone, Docker residue remains" case), it instead
sweeps every Docker resource carrying the stable deployment's label — the
same identity `install`'s own collision guard checks for the container,
volume, and network — plus any image under the stable project's name prefix,
which never blocks a new install (each install tags a fresh image under a
new random project name) but would otherwise linger as disk residue. A
foreign Docker object under a different label or name prefix is never
touched by either path. Either path leaves zero matching Docker resources
behind, so a following `install` always succeeds — `another local-live
Docker owner exists` cannot recur.

`update` refuses ambient Compose mode first, before touching anything, then
keeps the installed Compose volume and network and raises the store through
the offline migration ladder (`atelier2 migrate`, #244) in place, before the
new container starts. It refuses an installation whose identity has drifted
rather than guess at it. The previous container is stopped first to give the
ladder exclusive access to the store files; the ladder's own contract is the
backup — each step is one transaction that either commits completely or
leaves the file exactly as it was, so there is nothing to separately copy.

If the ladder refuses the store (an unknown or newer schema, or a locked
file), or the previous container fails to stop, nothing has happened yet: the
previous container is restarted untouched and `update` fails with the
refusal. `compose up` itself is not part of that safe window — the new
commit always changes the running container's labels, so Compose always
recreates it, deleting the previous container as an intrinsic part of that
one call, before startup can even be confirmed healthy. A failure at or
after that point therefore finds the previous container already gone:
`update` reports the true state instead — the store is migrated, the new
container's health is unconfirmed — and names `status`, then `uninstall` or
`update` again, as the recovery path. The durable record is untouched either
way until the very end. On full success the new container starts on the
migrated store and `update` reports the ladder's fingerprint proof alongside
the cockpit URL.

`update --fresh` is the previous behavior: `uninstall` followed by `install`
in one step, discarding the Compose volume and starting empty. It states
that plainly in its own output, and only when a volume actually existed to
lose — a sweep that only ever found a stray container or network never
claims a store was lost.

This slice deliberately has no copy, preview, activation, rollback, or
acceptance command. The stable console exposes current Core/V1 provider-free
behavior only; it adds no provider or Runner. Use the disposable candidate
above for zero-residue release proof.

Upgrading an installation made before this migration-preserving `update`
existed needs one `uninstall` first: the installation record gained the
volume and network's origin commit/tree as durable fields, and an
older-format record on disk cannot satisfy the new record's shape. `status`
on such a record reports `DRIFTED`; `uninstall`'s label-based sweep still
finds and removes it without needing to read it, so a following `install`
starts clean.

## Pin an executor toolchain

The atelier owns the executor copies it serves. The operator's daily CLI
(`~/.local/bin/claude`, `~/.local/bin/grok`, `~/.local/bin/codex`) may update
freely and is not the pin. Point `--claude-executable`, `--grok-executable`,
or `--codex-executable` at an atelier toolchain, not at those host binaries.

Install one already-conformant release into
`${XDG_DATA_HOME:-$HOME/.local/share}/atelier2-toolchains`:

```bash
uv run --locked python scripts/install_executor_toolchain.py --provider claude --version 2.1.233
uv run --locked python scripts/install_executor_toolchain.py --provider codex
uv run --locked python scripts/install_executor_toolchain.py --provider grok --from /path/to/the-conformant-grok
```

The script prints the absolute executable path. It imports
`CONFORMANT_CLAUDE_VERSIONS`, `CONFORMANT_GROK_VERSIONS`, and
`CONFORMANT_CODEX_VERSIONS` from the subscription adapters and does not keep a
second list. Claude's set has more than one member, so the fenced command
includes `--version` with one of them. After the tree lands, the script asks
the binary `--version` and refuses an answer that is not that selected member.

Claude and Codex are fetched with `npm install` into an isolated prefix
(`node_modules/.bin/claude` or `codex`). Grok is a standalone binary, not an
npm package: pass `--from` to a conformant executable and the script copies it
to `grok-<version>/grok`. `--from` copies an already-held executable into that
layout instead of fetching.

This script does not alter a running Serve, download during `serve`, or resolve
the executable path from admission. Those remain later slices of the toolchain
item.

## Raise an older store

Runtime startup still refuses every predecessor (`MigrationRequired`) and
does not alter the file. The offline command is the tool that refusal names:

```bash
atelier2 migrate --database /path/to/atelier.sqlite
```

Stop the process that owns the file first. The command refuses a write lock
it can see; an idle reader is not always visible, so stopping the serve is
the operator's gate. It does not create a store, does not start a server, and
does not open a runtime.

The file is inspected, then raised one published step at a time. Each step
ends with the fingerprint [ADR 0001](decisions/0001-durable-runtime.md) names.
Any doubt rolls the transaction back, so a failed hop leaves the predecessor
unaltered. Today the built steps run from schema version 13 to the current one, each
either an additive table home or a rebuild that copies every predecessor row. Older published predecessors, and unknown or future
versions, are refused by name. A store already on the current schema is left
unaltered and said to be already current.

## What this slice does not do

- **Live cutover.** The candidate selects no existing process, port, container,
  network or volume; it is not a replacement action.
- **Runner or provider execution.** The image supplies neither. A Start may
  reach existing Core `ExactOutput` behavior, but A.0 proves no external call.
- **CI image build.** CI checks the cheap recipe contract. The release/local
  gate, after a reviewed clean commit, builds, inspects, browses, restarts and
  tears down the candidate. Network hardening beyond its loopback publication
  stays with ADR 0009.

## Measure concurrent fake-executor load

This is a measurement of the current SQLite instance, not a capacity promise
and not a billed-provider run. CI keeps two concurrent runs so the suite stays
cheap. A larger local sweep reuses the same harness on one process:

```bash
ATELIER2_LOAD_CONCURRENCY=96 uv run --locked pytest --dist loadgroup -n 0 tests/integration/test_sqlite_load_measurement.py -s
```

`-n 0` keeps the instance on one worker. The report names the start door, the
event-write door, and one SSE reader per run, then the first observed pressure.
Raise `ATELIER2_LOAD_CONCURRENCY` until a named refusal appears; the 503 knee
was not reached at 96 on 2026-08-19 (`ed6376b`) and stays leftover.
Writer-lock, process spawn, watchdog cgroup, and memory are named only when
the harness observes them.

## Verification

Container recipes:

`uv run --locked pytest --dist loadgroup -n auto tests/tooling/test_container_packaging.py`

Stable local lifecycle:

`uv run --locked pytest --dist loadgroup -n auto tests/tooling/test_container_live.py`

Those jobs exercise the recipes and lifecycle scripts with a fake `docker`.
They do not build a real image.

Store migration:

`uv run --locked pytest --dist loadgroup -n auto tests/integration/test_store_migration.py`

Pinned toolchain:

`uv run --locked pytest --dist loadgroup -n auto tests/tooling/test_install_executor_toolchain.py`

Fake-executor load (CI n=2):

`uv run --locked pytest --dist loadgroup -n auto tests/integration/test_sqlite_load_measurement.py`
