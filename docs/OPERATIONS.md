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
external provider or Runner.

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
```

`success`, `cancel` and `resume` drive one full session each. `toolchain` and
`egress` drive no session at all: they measure the deployed image and the
Attempt network form, unbilled, with no credential and no provider call.

A session scenario is not its own launcher. The script starts one disposable
Core witness — the console's stand-in, attached to no network at all, so it can
reach nothing — writes one lease document, and hands the rest to the real
`atelier2-runner-launcher` described below. Everything a launcher does is
therefore proven here by the same code an installation runs, against a Core
this launcher did not start. Every engine operation the script itself still
performs goes through the launcher's own typed owner,
`atelier2.adapters.docker_carrier`, in argument vectors rather than shell
strings. What it calls the engine for directly is deliberately not a launcher
operation: the image builds, the `toolchain` and `egress` probe legs, and
`clean`'s own directory maintenance, which measure or tidy from the outside.

Each session scenario ends with one labelled Attempt network, one disposable
Core witness, one Runner, one handoff tmpfs volume, and one identity and one
journal volume. The launcher removes every object it created once the Runner
answers `RELEASED`. On failure they stay, named, plus the witness directory
under `/var/tmp/atelier2-301a-runner-witness.*`. The launcher unlinks the
client key as soon as the identity receiver has taken it, and the witness
unlinks its own disposable authority and Core keys afterwards, through held
directory FDs, keeping only public certificate metadata in that tree. Public
bootstrap reaches the Runner by container copy; Core reads the launcher's
inspect attestation from a read-only path. Core listens as
`core.runner-candidate.internal:8443` on that Attempt network only. The witness
CA hook is `tests/witness/runner_candidate_issuer.py`, a command line over the
same host owners; it is never copied into an image.

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
an internal one. Every container is created attached to *no* network, a
throwaway `CAP_NET_ADMIN` container installs that Attempt's policy inside its
network namespace and exits, and only then is it connected — so nothing ever
runs for even one unfiltered packet, and a policy that fails to install leaves
a container unable to reach anything rather than running wide open. That
network-none→policy→connect order is structural only for a container's first
start; `resume` instead restarts an already-attached container and reinstalls
the policy after it is back on the wire, safe only because the Runner's own
handoff wait keeps it silent until the policy lands (`runner_candidate.sh`
`crash-after-publish`) — giving `resume` that same structural guarantee is a
named open gap. The Runner itself carries no packet-filtering tool and no
capability to alter what was left. The Runner may reach outbound DNS,
outbound HTTPS, and its own Attempt subnet for Core. Core may reach nothing
outbound beyond that subnet: it holds the private key and the only store of
product truth and has no business on the Internet. Inbound, exactly one
opening exists in an Attempt: Core accepts its own session port from its own
subnet, because Core is the only container that
serves anything. The Runner accepts nothing inbound at all — it dials out, and
its answers return as established connections. Everything else, in either
direction, is REJECTed — including IPv6, which gets the same reject chain — so
a forbidden connection fails immediately with `Connection refused` and the
provider CLI's own error handling surfaces it, rather than a silent DROP the
operator would have to diagnose by timeout.

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
2026-08-23 operator ruling on `#540`). Start it with the lease directory the
console publishes into, the directory holding this installation's certificate
authority, and the image that installs an Attempt's packet filter:

```bash
uv run atelier2-runner-launcher \
  --lease-directory <directory> \
  --certificate-authority-state <directory> \
  --network-policy-image <image> \
  --attempt-root <directory> \
  --console-container <container>
```

**What it may do.** For each lease it claims — exclusively, by moving the lease
document — it creates that Attempt's network, attaches the console's own
container to it, creates the Attempt's identity, handoff and journal volumes,
starts the Runner container the attested manifest describes, hands it the
public bootstrap, mints the identity that one invocation may present, delivers
it through a receiver container, writes the inspect attestation Core admits the
session on, waits for the session, and removes every object it created. All of
them carry `atelier2.runner-lease=<lease>`. `--once` establishes a single lease
and stops; without it the launcher keeps watching at `--poll-seconds`.

**A lease is a request, not an authorization.** It names host directories this
privileged process will mount and the container it will attach to an Attempt
network, so it is validated against what *you* declared at start, never against
what the document claims: every path a lease names must resolve inside
`--attempt-root`, and its console container must be `--console-container`.
Anything else is refused by name before it is read, and what gets mounted is
the resolved path this checked, not the one the document spelled. That is what
keeps the seam safe when Serve becomes the writer of leases (ADR 0009 §2,
2026-08-23 amendment on ruling B) — Serve may ask for an Attempt and may never
command one.

**The attempt root holds per-Attempt material and nothing else.** Everything
under it is a surface some lease may ask to have mounted into an Attempt, so
`--certificate-authority-state` must lie outside it; overlapping trees are
refused at start rather than discovered by the lease that used them. Keep the
authority beside the attempt root, never inside it.

**What it may not do.** It never reads or writes the product's own store, never
runs provider code, never publishes a port or takes the host's own network
namespace — its own attestation refuses a container that was not created
reachable by nothing — and never removes an object that does not carry the
lease label of an Attempt it owns. The authority key stays in
`--certificate-authority-state` on this host: it is never mounted, copied, or
passed into any container, and the client key minted for an invocation is
unlinked the moment its delivery is over, taken or not.

**Failure shape.** A refused engine operation, a container that does not match
the manifest Core bound, or a Runner that exits nonzero with nothing retained in
its journal ends *that Attempt* loudly with a named refusal and leaves its
objects on the host to be read; it is reported as `attempt-failed=…`. A lease
this launcher will not accept at all — one naming something outside what you
declared, or a document that cannot be read — is refused where it is claimed
and reported as `lease-refused=…`; it stays quarantined under the lease
directory's `claimed`, never retried. Neither ends the launcher: the next lease
is served, because one bad Attempt is a bad Attempt and not an outage, and from
C-3 one document Serve got wrong must cost exactly one Attempt. A bounded
`--once` run exits nonzero when its lease failed.

The one restart the launcher performs is the opposite case: a Runner that exits
nonzero but did retain a terminal record still holds the only account of what
happened. That exact container is released from its Attempt network first,
started again, and only then policed and reconnected — the same order a first
start has, because a restart throws away the namespace the policy lived in.

Residue is reconciled, not swept: at start the launcher releases every lease
still marked claimed by a launcher that is gone and removes exactly the objects
carrying those leases' labels. An Attempt that may already have run is never
silently run a second time — what its owner does with an interrupted lease is
that owner's decision.

**Three bounds this form still has.** The console container a lease names must
already be attached to nothing: the launcher's own fence requires a container
created private, which the disposable witness Core is and the Compose console —
started on its own `serve` network — is not. Attaching the live console to an
Attempt network is therefore not yet proven and is C-3/C-4 territory; today the
attach path is exercised by the witness's stand-in. Run exactly *one* launcher
per lease directory: reconciliation identifies an abandoned Attempt by its lease sitting
in `claimed`, which a second launcher starting beside a working one would read
as abandoned (a launcher-ownership token is `#540` C-3 work, with the fleet it
belongs to). And a container may be attached to *one* Attempt in its lifetime:
the Attempt policy is appended to that container's own chains, so a second
Attempt's rules would sit behind the first's and quietly widen both. Rather
than accumulate silently the carrier refuses with
`carrier-attempt-policy-already-installed`, which today only a console attached
twice can trigger; giving each Attempt its own named chain, removed at release,
is `#540` A2 and is what a long-lived console needs before C-3 attaches it more
than once.

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

`update` is `uninstall` followed by `install` in one step. It refuses ambient
Compose mode first, before touching anything. Redeploying to a new commit
through `update` discards the Compose volume only when a volume actually
existed to lose; the command states that plainly in its own output — a
sweep that only ever found a stray container or network never claims a
store was lost. This is accepted while the stable console still holds no
durable operator data worth preserving; a real store migration is a later
concern.

This slice deliberately has no copy, migration, preview, activation, rollback,
or acceptance command. The stable console exposes current Core/V1
provider-free behavior only; it adds no provider or Runner. Use the disposable
candidate above for zero-residue release proof.

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
