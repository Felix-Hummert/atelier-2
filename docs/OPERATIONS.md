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

Each session scenario creates one labelled Attempt network, one disposable
Core witness, one Runner, one handoff tmpfs volume, and one identity and one
journal volume. Exact labelled objects are removed only after the Runner
answers `RELEASED`. On failure the script leaves those objects and prints
their names plus the witness directory under
`/var/tmp/atelier2-301a-runner-witness.*`. After the identity receiver
succeeds, the launcher unlinks host private keys through held directory FDs
and keeps only public certificate metadata in that tree. Public bootstrap
reaches the Runner by `docker cp`; Core reads launcher inspect attestation
from a read-only path. Core listens as
`core.runner-candidate.internal:8443` on that internal network only. The
external CA hook is `tests/witness/runner_candidate_issuer.py`; it is never
copied into an image.

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
`CONFORMANT_CLAUDE_VERSIONS`, and bubblewrap. Exactly two paths are writable —
`/tmp` and the provider configuration directory
`/run/atelier2-provider-config` — and both are `noexec,nosuid` tmpfs mounts, so
the provider child may write data there and may never execute it or gain
privilege from it. Which paths a child may touch, and with which right, is a
manifest fact: `RunnerManifestV1` carries the whole allowlist, the Runner
installs exactly that as its Landlock ruleset, and the launcher's inspect
attestation re-reads the mount flags of every writable entry. Widening the
surface therefore changes the manifest identity Core selected and refuses,
rather than passing unnoticed. Anything outside the allowlist is denied by
Landlock; a read-only entry denies writes even where the mount would allow
them; and an attested path this image does not have refuses before the child
starts.

**The Attempt network.** Each Attempt gets its own routed bridge network, not
an internal one, and a throwaway `CAP_NET_ADMIN` container installs that
Attempt's policy inside the started Runner's own network namespace and exits;
the Runner holds no packet-filtering tool and no capability to alter it.
Allowed: outbound DNS, outbound HTTPS, and traffic to the Attempt's own subnet
so the Runner reaches Core. Forbidden: every other outbound port and every
inbound connection, including from another Attempt — Docker keeps separate
user-defined bridge networks unable to reach one another. The failure shape is
the point: forbidden traffic is REJECTed, so a connection fails immediately
with `Connection refused` and the provider CLI's own error handling surfaces
it, rather than a silent DROP the operator would have to diagnose by timeout.
`egress` measures exactly that: a real name resolves, HTTPS connects, and
ports 80, 25 and every inbound attempt refuse in under a second.

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
records that run's labelled network only after `docker network create`
succeeds, so a recorded network's absence is the release proof; a directory
with no recorded network — including one whose witness is still mid-run — or
whose recorded network still exists, is left untouched. Failure-analysis
residue is never removed by `clean`. Clearing a released directory's
root-owned `core-store` needs the `atelier2-301a-core` image; `clean` refuses
with a named reason if that image is missing rather than pulling one.
`images` removes only the candidate images that are actually present, so
running it again after `clean` or a prune is a no-op, not a failure; it is
never run implicitly by `success` or `cancel`, so a normal run keeps reusing
Docker's build cache.

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
```

`status` is read-only and prints exactly `RUNNING`, `STOPPED`, `INCOMPLETE`, or
`DRIFTED`. `stop` and `start` first validate the complete record and then address
only its exact container ID; they never rebuild, recreate, search for a
replacement, or adopt a listener or Docker resource. A failed start stops that
same container and leaves the volume intact. A failed install removes only its
intent-owned project when exact identity can be proved; otherwise it leaves the
incomplete record and fails loudly.

This slice deliberately has no update, copy, migration, preview, activation,
rollback, acceptance, retirement, or uninstall command. Do not manually delete
an accepted record or its volume. The stable console exposes current Core/V1
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
