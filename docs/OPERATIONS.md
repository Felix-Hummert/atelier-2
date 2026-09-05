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
selected or changed. A rerun creates a new disposable candidate. This package
supplies no external provider or Runner. The entrypoint can carry an operator-declared
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

**A Runner does not die with the Serve that went away.** Losing the session
connection reaps nothing: the Runner keeps its provider child running and
redials the session port, so a Serve that comes back and resumes that Attempt's
workflow meets the same invocation again and receives the work it already paid
for, instead of an invocation lost to a restart. The recovered workflow needs
no session state of its own for this — it binds its listener, accepts that one
Runner again, and the cold session it builds is carried by the durable
attempt's own idempotency. Expect the Runner container to still be running
through a Serve restart; that is the healthy shape.

**One span bounds everything that Runner waits on.** The attempt span its
attested manifest declares is spent from the moment its session starts, and
every wait draws on that one budget: dialling Serve, the TLS handshake, and
each frame it waits on afterwards — so a Serve that completes the handshake and
then stops speaking cannot hold a Runner's provider child and credential
channel open indefinitely. When the span runs out the Runner reaps its child,
keeps whatever it had already journalled, and exits. That is both the outer
bound on how long a Runner container survives a Serve that never returns, and
the Attempt the convergence below is for.

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
bash scripts/container_live.sh reconcile
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
container's health is unconfirmed — and names `status`, then `reconcile`, as
the store-preserving recovery path. The durable record is untouched either
way until the very end. On full success the new container starts on the
migrated store and `update` reports the ladder's fingerprint proof alongside
the cockpit URL.

`reconcile` is that recovery: an interruption in `update`'s unprotected
window leaves a healthy new container running beside a record that still
names the deleted previous one, so `status` reports `DRIFTED` and every
exact operation refuses — and before `reconcile` existed the only exits,
`uninstall` and `update --fresh`, both discarded the store. `reconcile`
rebuilds the durable record from the one container of the recorded Compose
project, and publishes it only after the full exact verification proves that
container serves the recorded store volume at its frozen origin commit, on
the recorded engine and network, with the complete hardening, running and
healthy. It runs no Docker mutation and never touches the volume. Anything
it cannot prove — no or several project containers, another engine, foreign
labels, an unhealthy container — is a named refusal that changes nothing;
`uninstall` and `update --fresh` remain the store-discarding last resort.
After a successful `reconcile`, `update` proceeds store-preserving again.

`update --fresh` is the previous behavior: `uninstall` followed by `install`
in one step, discarding the Compose volume and starting empty. It states
that plainly in its own output, and only when a volume actually existed to
lose — a sweep that only ever found a stray container or network never
claims a store was lost.

This container installation has no automatic deploy path; `update` above is
always a hand command. The automatic auto-redeploy watcher below applies only
to the loopback host Serve installation.

### Loopback host Serve hand update

This section describes the loopback host installation, not the container
installation above. The host-process installation runs as the systemd user unit
`atelier2-serve.service`. From its clean `main` deploy checkout, one hand
command fast-forwards, installs the locked Python and frontend dependencies,
builds the frontend, stops the unit, backs up the live store, migrates it,
takes the checkout's own workflows into the live catalog, starts the unit, and
verifies that health serves the new commit:

```bash
bash scripts/serve_live_update.sh
```

The store lives under
`${XDG_DATA_HOME:-$HOME/.local/share}/atelier2/live-store`. Every update copies
`atelier.sqlite` and `external.sqlite`, plus the SQLite `-wal` and `-shm`
sidecars when present, into a timestamped `backups/pre-redeploy-*` directory
and verifies every copied size before migration. A migration refusal restores
and rebuilds the commit reported by the running Serve, restarts the Serve, and
still exits nonzero. With the Serve unreachable and no recorded deploy, the
command refuses and changes nothing. A `live serve is DOWN, operator action
needed` line means that recovery itself failed; inspect `journalctl --user -u
atelier2-serve.service -e` before acting.

After migration and before the unit restarts -- the Serve is still stopped, so
nothing else writes the store at the same time -- the command connects the
deploy checkout itself as a definition source (`workflows/*.yaml`, ref
`refs/heads/main`; connecting the same checkout and ref again is the same
source, so this step is idempotent across runs) and takes its workflows in.
Each path gets its own word in the log: `published` for bytes the catalog
gained, `present` for bytes it already held, or `refused` for the one path
that stopped that intake. A name already held by a manually imported lineage
is adopted rather than refused -- the source revision becomes that lineage's
new head and the manual revisions stay its history -- while a name any source
has already delivered still refuses. Bytes an unsourced lineage already
serves as its current revision under the same name are recognised as present
and gain the same provenance; the lineage id and its history are untouched. A
refusal does not hold the Serve
back -- it starts with whatever workflow catalog state it already had -- but
the command exits
`3` rather than `0`, distinct from the generic failure exit `1`, so
auto-redeploy's watcher can tell the two apart. It logs a warning naming the
served commit and the refusal, does not count a failure tick, and keeps the
unit green: the deploy itself succeeded, so nothing here should raise the
operator's failure streak toward its alert threshold. The refused path keeps
serving its previous catalog state until the next successful deploy or a hand
`atelier2 definition-source intake` fixes it. The source itself failing to
connect (an unreadable checkout or an unresolved ref, not a per-file refusal)
is treated like a migration failure instead: it rolls back to the previously
served commit and restarts that, and does count as an ordinary failure tick.

Install the clean-stop classification once beside the unit, as the same user:

```bash
mkdir -p ~/.config/systemd/user/atelier2-serve.service.d
cp scripts/atelier2-serve.service.d/clean-stop.conf \
  ~/.config/systemd/user/atelier2-serve.service.d/
systemctl --user daemon-reload
```

The drop-in makes the launcher's SIGTERM exit code 143 a successful stop, so a
deliberate update stop does not leave the unit failed. Installation does not
start, restart, or enable the live service; those remain explicit host actions.

The unit sets no `TimeoutStopSec`, so a `stop` waits systemd's default 90
seconds before SIGKILLing the process; the serve process itself bounds an open
Workbench tab's event stream to `SERVE_SHUTDOWN_CONNECTION_GRACE_SECONDS`
(`src/atelier2/host/serving.py`, 10 seconds), comfortably under that default so
a stop always finishes clean and runs `runtime.close()` regardless of how many
tabs are open. An operator who ever sets `TimeoutStopSec` on the unit must keep
it above that grace. 10 seconds is also long enough to let the longest
legitimate in-flight request -- the project-source connect POST reaching out
to a remote such as GitHub -- finish; cutting it mid-flight is acceptable
because the redeploy that triggers this grace already checked for running
runs before it started, and a cut connect is simply retried by the operator.

### Auto-redeploy watcher

**Auto-redeploy is the deploy path for the loopback host Serve above: a green
landing on `main` reaches it without an operator hand.** A systemd user timer
(`scripts/atelier2-auto-redeploy.timer`, a two-minute poll) runs
`scripts/auto_redeploy.sh`. The watcher serializes timer and hand runs in the
checkout's Git admin directory, fetches `origin/main`, and compares it with the
commit reported by live health. A matching commit is a no-op. Otherwise the
watcher requires a clean `main` checkout on its **tracked** paths -- an
untracked file or directory (the operator's own scratch files, a build
artefact) never blocks a deploy, because the deploy only ever fetches and
fast-forwards, which cannot touch anything git does not already track (#1186)
-- checks for running runs, then walks
`main`'s first-parent history back from the fetched commit (bounded to
`green_ancestor_search_depth` commits) for the newest commit with green
GitHub checks, so continuous merges landing faster than CI never starve live
delivery behind a HEAD that is always still checking; a commit older than
what is already served is never deployed. It checks for running runs again
immediately before the update, then hands the verified commit to
`scripts/serve_live_update.sh`. That script owns the fast-forward to the
verified commit and remains the one owner of build, backup, migration,
restart, and post-update health verification; the watcher never moves the
checkout itself. The watcher runs the **target commit's own**
`serve_live_update.sh` (materialised via `git show` into the checkout's Git
admin directory, then removed), never the copy already on disk, because Git
replaces a tracked file by unlink-and-create and a shell that already opened
the old file would otherwise keep reading it for the rest of the run. Staging
it under the Git admin directory, rather than the tracked checkout, means the
materialised file never shows up as an untracked file in `git status` and can
never itself cause the watcher's own clean-checkout preflight to refuse a
deploy.

Queued or running GitHub checks wait for another tick, unless an older commit
in the window is already green. No reported checks wait for up to 30 minutes
after the commit; after that they count as red. A run in
`STARTED` also waits without failing the unit; a run parked on a person --
`WAITING_INPUT`, `WAITING_RECONCILIATION` -- does not, because its answer is
taken under whichever `--application-version` the serve carries when the person
answers, so a redeploy cannot strand it
(`tests/integration/test_wait_survives_version_change.py`). An unreadable
health, run list, or check result fails closed. The watcher never deploys a
commit with a failed, cancelled, or timed-out check; completed neutral and
skipped checks are accepted as non-red.

Enable it once per host, from the deploy checkout, no root:

```bash
mkdir -p ~/.config/systemd/user
sed "s|/absolute/path/to/atelier-2|${PWD}|" \
  scripts/atelier2-auto-redeploy.service \
  > ~/.config/systemd/user/atelier2-auto-redeploy.service
cp scripts/atelier2-auto-redeploy.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now atelier2-auto-redeploy.timer
```

The Git admin directory holds `auto-redeploy.failures`, `auto-redeploy.busy`,
`auto-redeploy.last-alert`, and `auto-redeploy.last-busy-alert`. A successful
deploy or genuine no-op clears both streaks. A dirty tracked checkout or a
non-`main` checkout warns and increments the failure streak but leaves the
tree untouched. The third consecutive failure, and the first repeat at least
one hour later while the streak persists, logs at error priority and fails
that oneshot tick; other failure ticks exit successfully. Every deferred tick
names the runs it waits for -- public reference, state, and start time -- and
the tenth in a row, then at most hourly while the streak persists, repeats
that at warning priority; a busy tick never fails the unit. Inspect the
tagged journal and unit state with `journalctl --user -t atelier2-autodeploy
-e` and `systemctl --user status atelier2-auto-redeploy.service`.

A standing failure streak is also visible without the journal: on every tick
that resolves to a success or a failure, the watcher writes its own outcome
(`failure_count`, the last failure's reason and instant, the last success's
commit and instant) to `redeploy-status.json` beside the live database
(`${XDG_DATA_HOME:-$HOME/.local/share}/atelier2/live-store`); once
`failure_count` reaches three, `GET /health` names it as
`redeploy: {blocked_since, reason}`, and a status file that exists but does
not parse is named as unreadable rather than read as "no problem" (#1186).

### Live provider canaries

The billed loopback host process is installed as the systemd user unit
`atelier2-serve.service`. Its launcher owns the `atelier2 serve` executor
flags; the canary does not copy them. The installed canary unit does share the
launcher's effective runtime truth: installation substitutes the same deploy
checkout as its working directory, and `%h/.local/bin/uv run --locked` is the
same pinned interpreter and lock path that `serve-live.sh` uses. It asks that
running instance for its currently startable agent configurations, runs the
matching headless, workspace-tools, or atelier-doors workflow once with each
exact configuration hash, and refuses an admitted workflow whose hash differs
from the deploy checkout. Because the post-Serve-start drop-in fires this run
at process start rather than once it can answer, the run first polls `/health`
for up to 60 seconds until it answers `serving` -- failing loud with the last
health answer, and trying no vector, if it never does -- before any other
discovery step begins. A configuration is a vector when the listing's own
`startable` field says so, or when its only problem is a missing live
receipt (`not_startable_reason: provider-probe-receipt-missing`) -- both the
same judgment a start reads, computed once by the deployment's atomic
snapshot; discovery derives nothing of its own from either field. A
superseded revision (the model registry no longer points to it) carries its
own distinct reason and is excluded; a redeploy that invalidates every
registered configuration's receipt still leaves it reprobable. Discovery is
capped at four configuration pages, 50 known startable vectors, and 300
seconds. All distinct admitted workflow names resolve before any vector
starts. Every discovered vector then probes concurrently, up to 8 vectors'
own live billed runs in flight at once
(`PROVIDER_CANARY_MAXIMUM_CONCURRENT_VECTORS`), so one vector's own terminal
deadline never delays or blocks a sibling's receipt beyond that shared
worker cap -- each vector's outcome replaces its own receipt the instant it
is known, not in the order discovery listed it. Each vector still
has its own 300-second terminal deadline, while the complete process has a
15,300-second deadline enforced by both the runner and its systemd unit. Every
HTTP call has a 30-second cap reduced to the remaining discovery, vector, and
process deadline. The durable run owns provider output. The canary atomically
replaces only the secret-free
`provider-probe-receipt/v1` at
`${XDG_STATE_HOME:-$HOME/.local/state}/atelier2/provider-probes/live/<vector-id>.json`.

A receipt's validity key is a content digest of the provider layer
(`provider_layer_digest`: every provider adapter module, the Runner-side
CLI-pin registry `adapters/runner_cli_pins.py`, `host/provider_canary.py`,
and `contracts/provider_probe_receipts.py`), not the whole `source_commit`
(`source_commit` still travels on the receipt, but only as journal provenance,
#1124). A redeploy that leaves those files' bytes unchanged leaves every
receipt proven and every configuration immediately startable; only a
redeploy that actually touches the provider layer turns receipts over, and it
turns over all of them at once, since they share one digest. Narrower than the
full provider surface on purpose: the pinned CLI executable path
(`serve-live.sh`'s `--claude-executable`), the executor start-binding wiring
(`application/resolve_start_bindings.py` and the composition it feeds), and the
probe workflow bytes themselves (`workflows/provider-canary-*.yaml`) stay out
of the digest -- none of the three has a settings-independent path both the
canary and the Serve process can read and hash identically today. The
26-hour receipt validity is the backstop for that residual: a redeploy the
digest cannot see still turns every receipt over within one day. Each run's
own journal line names which of three outcomes happened:
`receipts kept (provider layer unchanged)`,
`receipts invalidated (provider layer changed: <digest8> → <digest8>)`, or
`no readable prior receipt (this run's provider layer: <digest8>)` when no
earlier receipt this runtime can read exists yet -- printed the moment
discovery finishes and the digest is known, before any vector starts, and
visible through `journalctl --user -u atelier2-provider-canary.service -e`.
Receipts remain valid for 26 hours, so the nightly schedule has two hours of
overlap. A receipt always says what the youngest probe attempt found: after a
vector enters its own execution, a failed attempt replaces that vector's
still-valid success before the next readiness read. Health, configuration
pagination, an empty list, or global workflow-name resolution belong to
discovery instead: their failure leaves every vector receipt byte-identical and
makes the oneshot fail loudly through its exit status and journal. A locally
unreadable workflow, hash mismatch, start refusal, timeout, or terminal failure
after vector entry replaces only that vector's receipt.

`POST /runs` uses the public `StartRunRequestResourceV2` form from the shared
run-command owner: workflow revision, one exact agent binding, and no orders.
That public start has no separate `idempotency_key`; `run_id` is its durable
idempotency identity. The timestamped id makes every timer or deploy trigger a
new run, including another trigger on the same day. The runner does not persist
the planned id before POST. A process crash after an accepted POST but before
the receipt therefore leaves a named duplicate-billing gap: the next trigger
uses a new id. Closing it requires persisting the planned `run_id` as the retry
key before POST and replaying that id until its outcome is receipted.

Install the oneshot, its nightly persistent timer, and the post-Serve-start
drop-in from the deploy checkout, as the same user that owns
`atelier2-serve.service`:

```bash
mkdir -p ~/.config/systemd/user/atelier2-serve.service.d
sed "s|/absolute/path/to/atelier-2|${PWD}|" \
  scripts/atelier2-provider-canary.service \
  > ~/.config/systemd/user/atelier2-provider-canary.service
cp scripts/atelier2-provider-canary.timer ~/.config/systemd/user/
cp scripts/atelier2-serve.service.d/provider-canary.conf \
  ~/.config/systemd/user/atelier2-serve.service.d/
systemctl --user daemon-reload
systemctl --user enable --now atelier2-provider-canary.timer
```

The timer's `Persistent=true` catches a missed 03:00 local run after the user
manager returns. The drop-in takes effect on the next Serve start. Its
`--no-block` keeps Serve from waiting for a billed run, and its `-` prefix keeps
Serve healthy when the optional canary unit is missing or refuses activation.
Start one probe without restarting Serve with:

```bash
systemctl --user start atelier2-provider-canary.service
journalctl --user -u atelier2-provider-canary.service -e
```

The canary workflows are admitted only after their budget revision exists on
the live instance. This is a landing operation, in this order:

1. Publish `workflows/budgets/provider-canary.json` with
   `POST /atelier/api/v1/budget-revisions` and retain the returned
   `budget_revision_hash`.
2. Replace the TODO head in each `workflows/provider-canary-*.yaml` with a node
   budget reference named `provider-canary` and that exact returned hash. The
   budget file's local SHA-256 is not a publication receipt.
3. Publish each resulting YAML document through
   `POST /atelier/api/v1/workflow-revisions`. Admit each returned workflow hash
   through `POST /atelier/api/v1/catalog-lineages` with
   `{"kind": "workflow", "catalog_revision_hash": "<hash>", …}`; when its
   authored name already owns a lineage, resolve that name through
   `GET /atelier/api/v1/catalog-revisions/by-name/workflow/<name>` and append
   through `POST /atelier/api/v1/catalog-lineages/<lineage-id>/members`
   instead.
4. Only after all three admissions answer with their exact workflow hashes,
   activate the deployed revision and start the canary oneshot. A partial
   publication is not activation authority.

Every publication and admission above targets the same loopback base URL the
installed `atelier2-serve.service` serves (normally
`http://127.0.0.1:8422`). The landing records the four returned revision hashes
in its own evidence; this runbook does not copy live hashes that change with
the published documents.

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

**Connecting a served project to live GitHub is `atelier2 connect`, once,
offline — it replaces the removed `--github-*` serve flags.** The connect act
records the source kind (`github`), the opaque source address
(`owner/name`), its nonidentity operating ref (`--source-ref base-branch`), a
credential-directory reference holding a `token` file, the auth method
(`personal-access-token`) and the connecting actor;
serve then composes the live `open-pr` adapter from that record whenever it
serves the connected project, with no GitHub flag on the serve line. A serve
started with the old flags is refused by argparse as unrecognized arguments.
For GitHub, the CLI requires the separate ref and refuses the legacy
`owner/name@branch` address; V44 migration relocates that embedded branch into
the row's private source-ref detail before V45 readers accept it.
The live composition still requires a loopback bind. An agent-authored
`open-pr` grant now uses the same durable reconciliation path as an Action:
an unknown GitHub readback pauses at the agent node for an operator decision,
rather than refusing admission or reporting completion before a receipt exists.

`connect` refuses when a project already has an active connection at a
different address of the same source kind, to catch a typo. When the source
genuinely moved — the connected repository was renamed or transferred —
`atelier2 connect --move ...` publishes two revisions in the same command:
the old address continues as `DISCONNECTED`, and the given address is
published `CONNECTED`; the command prints both revision numbers, and nothing
is deleted. The running serve read the connection once at startup, so it
needs one restart to pick up the move; the auto-redeploy performs that
restart on its next deploy, and there is no reason to force one sooner. That
restart blocks on an effect intent under the old address only while the DBOS
workflow that owns it is still open: an open workflow must finish or be
reconciled first, but once it has ended, history under the old address never
blocks the restart, whatever that intent's own recorded state.

### Publish the issue-to-pr catalog

`serve_live_update.sh`'s Git-source intake admits only `workflows/*.yaml`; a
schema, budget, grant, or adapter operation a shipped workflow pins is never
picked up by that intake and must be published by hand before the workflow
that pins it can start. For `workflows/issue-to-pr.yaml`, this is a landing
operation, in this order:

1. Publish its three schemas --
   `workflows/schemas/issue_to_pr_candidate_report.json`,
   `workflows/schemas/code_review_result.json`, and
   `workflows/schemas/issue_to_pr_release_decision.json` -- through
   `POST /atelier/api/v1/schema-revisions`, one call per document.
2. Publish `workflows/budgets/push-implement.json` through
   `POST /atelier/api/v1/budget-revisions` if the live catalog does not
   already carry it (`push-before-open-pr` publishes the same budget).
3. Publish the two adapter operations through
   `POST /atelier/api/v1/adapter-operation-revisions`: `open-pr` is exactly
   the bytes `{"operation":"open-pr"}`; `push-atelier-commit` carries this
   deployment's own author and committer identity, so it has no canonical
   bytes here.
4. Publish the two tool grants through
   `POST /atelier/api/v1/tool-grant-revisions`, after step 3: the
   `run-project-verification` grant is exactly the bytes
   `{"capability":"run-project-verification"}`; the `push-atelier-commit`
   grant names step 3's operation by its own returned hash, so it cannot be
   published first.

The Git-source intake admits `workflows/issue-to-pr.yaml` regardless of this
order; it does not refuse the document for an unresolved pin. The admitted
revision reads `executable: false`, with `not_executable_reason` naming the
first pin still missing, until every schema, budget, grant, and operation
above is published -- publishing the missing ones then turns the same
revision `executable: true` in place, with no new intake. The order above
still matters: the `push-atelier-commit` grant names step 3's operation by
its own returned hash, so it cannot be published first. The live hashes are
the landing's own evidence; this runbook does not copy them, for the same
reason the canary's four hashes above are not copied either.

### Publish a queue policy with its cap and its automation label

One CAS-guarded call names both of a project's queue rules at once:

```bash
curl -fsS -X PUT \
  http://127.0.0.1:8422/atelier/api/v1/projects/<public-project-reference>/queue-policy \
  -H 'Content-Type: application/json' \
  -d '{"revision_number": <previous + 1>, "expected_revision": <previous>,
       "maximum_active_runs": 2, "automation_label": "bereit"}'
```

`expected_revision` is the revision number currently in force (`0` for a
project that has never published one) and `revision_number` is that plus one;
a mismatch is refused as `queue-policy-revision-conflict` rather than
overwriting a revision someone else published. Revisions are append-only, so
changing either rule means publishing the next revision, never editing this
one.

`maximum_active_runs` caps how many runs of this project may be active at
once; the rest wait in priority order. `automation_label` names the one label
that admits an item automatically: at the next sweep, every inspected proposal
whose tracker item carries that label is admitted under the `AUTOMATION_RULE`
authority and starts within the cap. The label is read from the tracker at
that moment, so removing it in the tracker before the sweep withholds the
admission; a human sets it there, and the atelier never writes it. Spell it
exactly as the tracker spells it, capitalisation included: `Bereit` and
`bereit` are two different labels here, and a policy naming one admits nothing
carrying the other. Omitting the field (or sending `null`) turns automatic
admission off, and `"*"` is refused: the policy names one label, and "admit
everything" is not a ruled value.

Two things this does not do. It admits nothing that has no inspected proposal
yet -- the label says "go", never which workflow or priority to go with, so
plan the item through `PUT /queue-proposals` first -- and it never overrides a
proposal marked `HUMAN_REQUIRED`. The sweep runs at Serve start, so a policy
published against a running Serve takes effect at its next start.

### A red project verification's own output (#1137)

When the redeemed `run-project-verification` grant exits nonzero, the
attempt's node receipt no longer names only an exit code. It names the exact
command, how long it ran, pytest's own short summary line where the retained
tail carries one, and -- when the check printed anything at all -- the
address of an artifact holding the last 64 KiB of its combined stdout and
stderr, e.g. `project-verification-failed: exit 1; uv run --locked pytest …;
after 806 s; 3 failed, 5961 passed in 45.23s; output artifact
sha256:<hash>`. That artifact is the same content-addressed material `POST
/artifacts` publishes and `GET /artifacts/{hash}` reads back (#1089); no
second store and no new wire concept carry it, and any credential shape
`redact_credentials` recognises is replaced before the tail is kept. A
verification that exits zero keeps no artifact -- the outcome's own hash and
summary are proof enough for a check that passed.

### An attempt that changed nothing, and the patch a red check rejected (#1156)

Before a grant is redeemed, the tree standing in the attempt's leased directory
is written into the project's candidate store and compared with the pinned tree.
Only an attempt about to redeem a grant is asked: a node that pinned none may
honestly answer without touching a file, and a reviewer judging a candidate is
exactly that. Equal means the attempt changed nothing: it ends in seconds, `FAILED` under
`CANDIDATE_UNCHANGED`, with no verification started and no grant redeemed, and
the node receipt reads `candidate-unchanged: the workspace still holds the
pinned tree <tree>, so this attempt changed nothing; the agent answered: ...`.
That answer is bounded and credential-redacted, and it is the point of the line:
three live `issue-to-pr` runs each paid ten minutes of project tests and ended
`PROJECT_VERIFICATION_FAILED` on what was almost certainly the pinned tree, so
an answer claiming work that is not there is now stated instead of absorbed. No
candidate ref is written for this ending -- naming a tree is not keeping one.

Where the tree did change and the check then exited nonzero, the receipt names a
second artifact beside the output tail: the attempt's own patch against the
pinned tree, bounded to 64 KiB from its start and redacted the same way, as
`candidate diff artifact sha256:<hash>`. That receipt also carries the schema
revision and the value hash of the answer the provider gave, so what the builder
said is readable through `GET /artifacts/{hash}` as well. The rejected work is
still not kept as a candidate: what survives is evidence, not something a later
run could take.

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

The pinned grok applies a declared output schema to *every* assistant message
and ends the session at the first message that carries no tool call. A Grok node
bound to `headless_with_tools` has to narrate and act before it answers, so that
vector hands the CLI no schema at all: the declared shape closes its job in
words, and the answer is judged against that schema at the output seam, where an
answer that is no such document fails the attempt as `OUTPUT_SCHEMA_REFUSED`.

Such a node can still end without a candidate for a reason that is neither a
crash nor a failed check: a session that answered without opening a single door
is refused as a provider failure rather than published as a report about work
nobody did. The node ends FAILED, and only an explicit operator replacement runs
it again. In the receipt it appears as a failed attempt whose transcript ends on
one assistant turn and no tool call. Seeing it repeatedly for one node is a
signal about that node's instruction, not about the deployment.

### Arm the Claude executor a builder needs

Pinning the executable serves one executor: `claude-subscription/v1`, a
tool-free call that can read no file. Two more are armed by name beside it,
and only one of them is a builder.

* `--claude-workspace-tools` arms `claude-subscription-tools/v1`. This is the
  only Claude executor whose invocation reaches the attempt's own workspace,
  so it is the only one a node that pins `run-project-verification` or
  `push-atelier-commit` can be cast onto.
* `--claude-atelier-doors` arms `claude-atelier-doors/v1`. Its tools are the
  atelier's own API doors and it removes every built-in with `--tools=`; it is
  the conductor's executor and touches no file of the project.

None of the three hands Anthropic the output schema its node declared. The API
refuses a schema whose root is an `allOf`, `anyOf` or `oneOf` as a tool's input
schema, and `code_review_result` -- what every reviewer of `issue-to-pr`
declares -- is exactly such a document, so a Claude review node died after
seconds with `api_error: API Error: 400` and no model was ever reached. The
declared schema now closes the job in words, and the answer is judged against
it at the output seam. A node whose answer carries no value that schema admits
fails there as `OUTPUT_SCHEMA_REFUSED` after its one repair round, which reads
in the receipt as a refused output rather than as a provider error.

Without `--claude-workspace-tools`, this deployment has no Claude builder for
`workflows/issue-to-pr.yaml`: its build node pins both grants above, and a
start that casts a Claude role onto either of the other two executors is
refused before the run exists
(`DurableAgentExecutorWithoutWorkspaceFileTools`, answered over the API as
`agent-executor-binding-unavailable`). Arming is an operations step of its own
-- add the flag to `serve-live.sh` and restart
`atelier2-serve.service` -- and, like every executor arming, it widens what a
billed run may do on this host. The flag is only half of it: the builder
model's registry row must also resolve to an agent configuration published on
`claude-subscription-tools/v1`, because a row still naming a tool-free Claude
is cast onto an executor that reaches no file, and the start then answers
`agent-executor-binding-unavailable` with the flag already in place.

## Connect a git definition source, see where it stands, and take it in

Three offline commands against a store that already exists. Only `intake`
publishes, and `serve` performs none of them at startup: a newer version of a
workflow enters the catalog because the operator asked for it.

```bash
atelier2 definition-source connect --database /path/to/atelier.sqlite \
    --location /srv/definitions.git --ref refs/heads/main \
    --select 'workflows/*.yaml=workflow' --actor felix
```

`connect` refuses without writing when the location is no repository or the
ref resolves nowhere -- there is no way to disconnect a source yet, so a wire
to nowhere is never registered. It answers for those two and no more: a
selection matching nothing today is an ordinary thing to configure, and every
selection problem surfaces at scan. It then records the repository, the ref,
and the selections,
and prints the source id every later command names. A selection is
`PATTERN=KIND`; the kind is configured, never guessed from the repository's
layout
([ADR 0018](decisions/0018-plugin-intake-and-neutral-roles.md)). The one
wildcard is `*`, matching inside a single path segment. Connecting the same
repository at the same ref again is the same source, not a second one.

```bash
atelier2 definition-source scan --database /path/to/atelier.sqlite \
    --source-id <id>
```

`scan` resolves the ref to one commit, reads every selected file of it, and
prints that commit followed by one line per path: `source_ahead` when the
catalog does not hold these bytes, `in_sync` when it does, and `source_absent`
for a path the catalog holds that the source stopped carrying. It writes
nothing at all, so a scan never changes what a run would use.

The location must *be* the repository -- a bare repository, a checkout, or a
linked worktree. A directory that merely lies inside one is refused, because
reading it would read a repository the operator never named. All three
commands refuse before writing anything, in one closed vocabulary:
`definition_source_unreachable`, `_ref_unresolved`, `_layout_unrecognized`,
`_selection_ambiguous`, `_path_escapes_repository`, `_no_selected_files`,
`_symlink_selected`, `_gitlink_selected`. A selected file the publication door
would refuse is reported in that door's own words, and stops the scan.

```bash
atelier2 definition-source intake --database /path/to/atelier.sqlite \
    --source-id <id> --actor felix
```

`intake` takes one commit of the source into the catalog. It reads the same
files `scan` reads, then publishes every one of them, admits it under the name
its document authored, and records where it came from -- source, commit and
path -- in one transaction. A refusal anywhere in the batch writes nothing at
all, so a failed intake leaves the catalog exactly as it was.

It prints the commit followed by one word per path: `published` when the bytes
entered the catalog, and `present` when the catalog already held them under the
name they author, which is why taking the same commit in twice writes no second
row. `refused` names the one path that stopped the whole batch: an authored
`name` outside the catalog's `[a-z][a-z0-9._-]*`, a name another lineage
already holds, bytes that already belong to another lineage -- including the
same bytes catalogued under a different name -- or a retired lineage. Pass
`--source-position <commit>` to take in exactly the commit a scan showed; a ref
that moved in between is refused rather than published unseen.

A path that is taken in a second time joins the lineage its earlier revision
belongs to, so an edited file becomes the next revision of the same catalog
entry and the revision before it stays exactly what it was. Continuity is the
repository path, never the authored name: renaming a file in the source starts
a new lineage.

Retire a lineage of any kind through
`POST /atelier/api/v1/catalog-lineages/<lineage-id>/retirements`. Repeating it
answers 204 again, and afterwards
`GET /atelier/api/v1/catalog-revisions/by-name/<kind>/<name>` answers
`catalog-lineage-retired` rather than a revision. Retirement takes the name out
of the live catalog and nothing else: every published revision stays readable by
its hash, and a run already under way keeps running.

Retiring an agent lineage does not yet stop a workflow start. A workflow
document references no agent definition today -- an agent reaches a run through
the role binding the start supplies, which names an agent configuration
revision and never a catalog name -- so there is nothing for a retired agent
name to refuse. Making such a start refuse by name needs that reference to
exist first; it is a named gap on #66, not a promise this door keeps.

Private repositories, project-scoped registration, disconnecting a source, and
the catalog's own Connect and Pull buttons are named absences, not oversights.

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

## Remove defective rows from the live store

A row the runtime can no longer read is removable without asking first; the
[prototype stage](PRODUCT.md) carries that ruling and its boundary. This is
the procedure, and it is a hand operation on the loopback host Serve's store
under `${XDG_DATA_HOME:-$HOME/.local/share}/atelier2/live-store`.

1. Stop the auto-redeploy timer, then the Serve, so nothing restarts the
   process into the middle of the write:

   ```bash
   systemctl --user stop atelier2-auto-redeploy.timer
   systemctl --user stop atelier2-serve.service
   ```

2. Copy `atelier.sqlite` and `external.sqlite`, plus their `-wal` and `-shm`
   sidecars where present, into a fresh timestamped directory beside the
   redeploy copies in the store's own `backups/`. This copy is the only way
   back; a deletion has no rollback of its own.

3. Delete in one transaction, children before parents. The immutability
   triggers refuse the delete by design, so the transaction drops the
   `*_no_delete` triggers that stand in the way, deletes, and recreates them
   with exactly the text `_PRODUCT_TRIGGERS` in
   `src/atelier2/adapters/dbos/schema.py` defines — that module is their
   owner, and a trigger recreated from memory or from an older copy leaves the
   store lying about what it protects. `PRAGMA foreign_key_list(<table>)`
   names each table's parents, so the delete order follows the walk from the
   leaves inward; a broken run's own row is the last to go.

4. Prove the store before starting anything: `PRAGMA integrity_check` and
   `PRAGMA foreign_key_check` must both come back clean, and the trigger set
   must be complete again.

5. Start the Serve and the timer again, then prove the repair from outside:
   `GET /atelier/api/v1/runs` answers the full list, and
   `journalctl --user -u atelier2-serve.service` stays quiet where the broken
   rows used to write a projection failure on every poll.

6. Say it in the report — what was removed, why it was unreadable, and where
   the backup stands. A removal nobody reads about is the silent kind the
   stage does not permit.

## What this slice does not do

- **Live cutover.** The candidate selects no existing process, port, container,
  network or volume; it is not a replacement action.
- **Runner or provider execution.** The image supplies neither; A.0 proves no
  external call.
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

## Land a pull request

**`gh pr merge --auto --merge` queues a pull request; it does not merge it on
the spot.** GitHub's merge queue builds a merge candidate from one or more
armed pull requests, runs `ci.yml` once against that candidate on the
`merge_group` event, and merges the pull request only once the ruleset's
required checks report green on that run; a red candidate leaves its pull
request out of the queue instead of blocking the ones behind it. This
replaces re-arming a pull request by hand after every trunk landing: the queue
absorbs a `main` move by rebuilding the candidate itself, instead of leaving a
`BEHIND` pull request to `cancel-in-progress` its own in-flight run.

The one-time ruleset step this depends on -- turning on "Require merge queue"
on the `main-protection` ruleset (merge method `merge`, group size cap 5,
admitting only non-failing pull requests) -- is an operator/head step done
once, through `gh api`, after this change lands; the queue has no effect on
pull requests opened before that step runs.

## Dead-code gates

Two gates keep code that nothing reaches out of the tree, and they do not yet
ask the same question of a test.

`uv run --locked python scripts/check_dead_code.py` runs vulture over
`src/atelier2` alone: a symbol only its own test reaches is not a symbol the
product uses, so it is dead. `npm run check:dead` (in `frontend`) runs knip
over the cockpit's `src`, where an unused file, export, or dependency is red --
but knip's vitest and playwright plugins register the test files as entry
points, so an export only a cockpit test imports counts as reached. Making the
cockpit gate ask what vulture asks turns roughly a dozen test-only exports red
and is its own slice, owned by #1168 (finding 12).

A vulture finding survives only by standing in one of three files, and which
file it stands in is the whole justification:

- `.vulture_allowlist.py` -- a production site *does* reach the name and vulture
  cannot see that site: a program built as text, a vocabulary the wire selects
  by value, a field read by a generated `__eq__` or by `asdict()`, a framework
  attribute. The entry names that site. If you cannot name one, the name does
  not belong here.
- `vulture_pending.py` -- the name waits for a decision an open item already
  owns. The entry carries an expiry and the gate turns red once it passes, so a
  parked decision stays slow rather than becoming permanent.
- `vulture_frozen.py` -- the name is built ahead of its caller and is kept
  (operator ruling 04.09.2026: freeze, do not throw away). No expiry; the entry
  names the open item that owns the caller. The gate lists these on every run
  without failing, and frozen means no hardening and no new tests -- the tests
  it already has keep running.

An entry naming something the gate no longer reports is red too: when a caller
arrives, or the code goes, its entry goes with it.

A third static gate in the same `quality` job, `ruff check --select ANN401`
scoped to `src/atelier2/contracts`, `ports`, `application`, and `api`, proves
only that none of those four packages accepts a parameter typed directly as
`Any` -- it says nothing about a nested `dict[str, Any]` or about return
types.

## The duplicate ratchet

`uv run --locked python scripts/check_architecture.py` also refuses copied
code. It reads every function of `src/atelier2` long enough to be recognised
again as five-token shingles, with its literals and its own names normalised,
so a copy someone renamed and reflowed still matches; a pair whose shingles
overlap by 95 per cent or more is the same code. `duplicate_baseline.toml`
names the pairs this tree already carries. A pair that is not listed turns the
gate red, and so does an entry whose pair is gone -- a list that only grows
stops describing anything. Resolving a listed pair therefore means giving the
two one owner *and* deleting its entry.

## SonarCloud and CodeQL

`sonar-project.properties` at the repository root configures SonarCloud's
analysis of the public project `overnightworks_atelier-2` (organisation
`overnightworks`, Free plan): source, test, and exclusion layout, plus the
rule classes marked won't-fix by #1203's triage. CodeQL's default setup,
enabled directly on GitHub, scans Python, JavaScript/TypeScript, and Actions
on the same pushes. Neither is a required check yet -- that follows the
measurement week described in #1203, which compares Sonar's findings against
the duplicate ratchet above and the `C901` complexity count.

Automatic Analysis cannot read coverage, so analysis runs from CI instead
(ruling 05.09.2026, #1203): the `quality` job's pytest run writes
`reports/coverage.xml` (`pytest-cov`) and the `frontend` job's vitest run
writes `reports/frontend-coverage/lcov.info` (`@vitest/coverage-v8`); the
`sonar` job downloads both and runs `sonarqube-scan-action` with the
repository's `SONAR_TOKEN` secret. Its scan step is `continue-on-error` until
the operator turns Automatic Analysis off in the SonarCloud project settings
-- SonarCloud refuses CI-based analysis while Automatic Analysis stays
enabled. `sonar` is not a required check.

## Code rules: gates, metrics, audit

The code rules in [`AGENTS.md`](../AGENTS.md) fall into three classes, and this
section only says which class a rule is in; `.github/workflows/ci.yml` stays the
live list of what actually runs.

Machine-checkable rules are gates there. Running today: the architecture check
(`scripts/check_architecture.py`, package boundaries), the duplicate ratchet
above, and the dead-code gates above. Dispatched and not landed yet: `ruff
check --select ANN401` over `contracts`, `ports`, `application` and `api`
(#1196). The size, complexity and narrative checks are ruled but unbuilt, and
the core-test-import ratchet starts only once the first adapter-bound test
module has moved.

Rules about the shape of a change — slice size, context-file length, the
adapter-import share in core tests — stay reported metrics and never become
gates, because a check cannot judge a cut. Everything a machine cannot judge is
ruled to run as a scheduled agent audit on the self-hosted runner, producing one
distributor issue per run (operator ruling 04.09.2026); that workflow does not
exist yet.

After a route change, regenerate the frozen OpenAPI document with `uv run
python scripts/write_openapi_frozen.py` before committing; its `--check` twin
becomes a CI gate once #917 wires it into `ci.yml`.

## Verification

Container recipes:

`uv run --locked pytest --dist loadgroup -n auto tests/tooling/test_container_packaging.py`

Stable local lifecycle:

`uv run --locked pytest --dist loadgroup -n auto tests/tooling/test_container_live.py`

Those jobs exercise the recipes and lifecycle scripts with a fake `docker`.
They do not build a real image.

Auto-redeploy watcher (against a real local git repository pair and doubles
for `container_live.sh` and the health endpoint):

`uv run --locked pytest --dist loadgroup -n auto tests/tooling/test_auto_redeploy.py`

Store migration:

`uv run --locked pytest --dist loadgroup -n auto tests/integration/test_store_migration.py`

Pinned toolchain:

`uv run --locked pytest --dist loadgroup -n auto tests/tooling/test_install_executor_toolchain.py`

Fake-executor load (CI n=2):

`uv run --locked pytest --dist loadgroup -n auto tests/integration/test_sqlite_load_measurement.py`
