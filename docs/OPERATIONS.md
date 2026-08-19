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

## What is packaged

A repository `Dockerfile` bakes the locked uv project and the built
`frontend/dist` into a slim image and runs as a non-root user. The image
carries exactly one provider executable: the Claude CLI pinned to the
conformant version the subscription executor already attests. Codex and Grok
are not in the image.

Durable store and attempt scratch live on the host at
`${XDG_STATE_HOME:-$HOME/.local/state}/atelier2` and are bind-mounted. Code
is not mounted: a redeploy is an image rebuild, so a container swap replaces
the code atomically.

The process binds `127.0.0.1:8422`. Compose uses the host network so that
loopback bind is the host loopback. A bridge `ports:` mapping is refused by
construction, because it would require binding `0.0.0.0` inside the container
and the billed-provider loopback rule would then refuse to serve.

The named process loggers share one JSON object per line on stderr; the root
logger is not configured. The two product sentences a diagnosing agent can
query are a failed agent attempt
(`agent_attempt_failed`, with `run_id` / `node_id` / `attempt_id`) and an
unhandled HTTP exception (`http_internal_error`). The uvicorn access log is
off: it has no reader. Run facts, including why a run stopped, belong on the
run resource, not in this journal.

## Credentials

Authentication is never baked into the image, its layers, logs, fixtures, or
this file. The start script mounts one regular host file named
`.credentials.json`, read-only, into an otherwise empty credential directory.
The container `HOME` is isolated. The operator's `~/.claude` tree, settings,
and any other provider home are not mounted.

Set `ATELIER2_CLAUDE_CREDENTIALS` to that file before starting. The script
refuses a missing path, a directory, a symlink, or a file of any other name,
because a missing bind source would make Docker create a directory on the
host.

## Start and redeploy

```bash
export ATELIER2_CLAUDE_CREDENTIALS=/path/to/.credentials.json
bash scripts/container_up.sh
```

The script creates the state root and its `store` and `scratch` directories
at mode `0700`, builds the image for the current operator uid/gid, and starts
the compose service. Rerun it after a landing to redeploy. It does not start
autonomy and it does not arm anything.

## Pin an executor toolchain

The atelier owns the executor copies it serves. The operator's daily CLI
(`~/.local/bin/claude`, `~/.local/bin/grok`, `~/.local/bin/codex`) may update
freely and is not the pin. Point `--claude-executable`, `--grok-executable`,
or `--codex-executable` at an atelier toolchain, not at those host binaries.

Install one already-conformant release into
`${XDG_DATA_HOME:-$HOME/.local/share}/atelier2-toolchains`:

```bash
uv run --locked python scripts/install_executor_toolchain.py --provider claude
uv run --locked python scripts/install_executor_toolchain.py --provider codex
uv run --locked python scripts/install_executor_toolchain.py --provider grok --from /path/to/the-conformant-grok
```

The script prints the absolute executable path. It imports
`CONFORMANT_CLAUDE_VERSIONS`, `CONFORMANT_GROK_VERSIONS`, and
`CONFORMANT_CODEX_VERSIONS` from the subscription adapters and does not keep a
second list. If that set has more than one member, the command refuses and
lists them; pass `--version` with one. After the tree lands, the script asks
the binary `--version` and refuses an answer that is not that selected member.

Claude and Codex are fetched with `npm install` into an isolated prefix
(`node_modules/.bin/claude` or `codex`). Grok is a standalone binary, not an
npm package: pass `--from` to a conformant executable and the script copies it
to `grok-<version>/grok`. `--from` copies an already-held executable into that
layout instead of fetching.

This script does not rewrite `atelier2-live.service`, does not download during
`serve`, and does not resolve the executable path from admission. Those remain
later slices of the toolchain item.

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

- **Live cutover.** The host unit `atelier2-live.service` stays the live
  serve until the operator switches it. The start script refuses to run while
  that unit is active and never issues `systemctl start`, `stop`, or
  `restart`.
- **Network hardening.** Reachability, exposure declaration, and anything
  beyond this machine stay with ADR 0009, which is not implemented. Host
  networking plus a loopback bind is packaging, not that decision.
- **CI image build.** The workflows do not build the image. A full
  frontend, Python, and Claude install on every pull request is not justified
  by what the image would prove: the serve still needs a real credential to
  start a billed provider, and the cheap contracts (non-root, pin, mounts,
  loopback, no secrets in the recipe) are checked as files.

## Verification

Container recipes:

`uv run --locked pytest --dist loadgroup -n auto tests/tooling/test_container_packaging.py`

That job reads the recipes. It does not build or run the image.

Store migration:

`uv run --locked pytest --dist loadgroup -n auto tests/integration/test_store_migration.py`

Pinned toolchain:

`uv run --locked pytest --dist loadgroup -n auto tests/tooling/test_install_executor_toolchain.py`
