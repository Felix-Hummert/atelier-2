# Operations

Audience: the human operator deciding how this installation is started,
stopped, or redeployed.

This file owns that runbook. It does not own product intent, requirement
sentences, or trust-boundary decisions. [PRODUCT.md](PRODUCT.md) says what
exists; [ADR 0009](decisions/0009-runner-trust.md) owns network hardening and
reachability; this file only says how the packaged process is started.

No operations owner existed. [docs/README.md](README.md) now names this file
for that question. Journeys are unwritten and would illustrate, not bind.

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

`uv run --locked pytest --dist loadgroup -n auto tests/tooling/test_container_packaging.py`

That job reads the recipes. It does not build or run the image.
