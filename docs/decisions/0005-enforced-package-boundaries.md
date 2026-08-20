# ADR 0005: CI enforces package boundaries

- Status: ACCEPTED 2026-08-13 — implemented: the gate landed with this record and
  runs in the quality lane on every pull request

## Context

Atelier 2 has distinct host, API, adapter, application, port, and contract
owners, but prose and review alone cannot prevent imports from reversing those
dependencies. A boundary gate must catch real import paths, unexpected new
top-level packages, and ownership of DBOS, SQLAlchemy, JSON Schema evaluation,
and PyYAML without replacing a maintained import-analysis tool with project code.
It must also fail loudly if configuration disappears or a source scan
unexpectedly shrinks.

Import Linter 2.13 and Tach 0.35.0 were measured on the same source tree at the
first landing of this record. Import Linter rejected the tested inward,
external-owner, and empty-package violations at the smaller dependency cost.
Tach omitted import-free modules from its map and accepted the empty-package
violation. Import Linter therefore covers the required import analysis. Its own
scan still remains green after a deleted leaf, so a narrow repository preflight
owns the positive source floor and exact reviewed contract metadata, and the
AST checks that Import Linter cannot see.

## Decision

`pyproject.toml` is the executable owner of the Import Linter contracts the
quality lane runs. The wrapper requires that named set to match exactly, then
runs its own preflights, then delegates import analysis to Import Linter. The
layer contract is exhaustive, treats API and adapters as peers, and permits
dependencies only toward the right of this derived view:

<!-- architecture-contract-view:start -->
```text
contracts: layers, root-facade, dbos-owner, wire-projection-split, route-vocabulary, schema-owner, yaml-owner
layers: __main__ > host > api | adapters > application > ports > contracts
dbos-owner: atelier2.adapters.dbos
yaml-owner: atelier2.adapters.yaml_workflows, atelier2.adapters.markdown_agent_definitions
root-facade-forbids: __main__, host, api, adapters, application, ports
```
<!-- architecture-contract-view:end -->

The root facade cannot bypass ports through a package or descendant import.
Only `atelier2.adapters.dbos` may import DBOS or SQLAlchemy. Only the YAML
adapters named above may import PyYAML; application code that reaches the
library directly fails the yaml-owner contract. JSON Schema evaluation stays
inside its profile owner. Wire schemas name no port type, including a port
re-exported through application. Routes name no port type.

The wrapper's source-module floor is the landed production inventory counted
from `src/atelier2/**/*.py`. Growth is allowed; shrinkage fails that floor
before any later import of the tree. The AST preflights refuse a port that
words an HTTP answer, an API module that names a port read-model, a use-case
record field that resolves to a port, and a route that reaches a port —
including the same reach nested under `api/routes/<group>/`.

The quality lane runs `uv run --locked python scripts/check_architecture.py`
immediately after dependency installation. The wrapper then delegates to Import
Linter with caching disabled and timings visible. The wrapper and
`uv run --locked lint-imports --no-cache --show-timings` keep the same contract
set and must report the same kept and broken counts. Shrinkage below the
measured floor requires a deliberate remeasurement of the floor in the wrapper.

The derived view above is checked byte-for-byte against a deterministic
rendering of the executable configuration; it is not an independent policy
owner. Contract identifiers, owner modules, and the source floor live in the
executable configuration and the wrapper, not as independently maintained
counts in this record.

## Consequences

- Forbidden inward imports, root-facade bypasses, DBOS/SQLAlchemy/PyYAML
  ownership violations, a wire or route that names a port, undeclared top-level
  packages, and the AST reaches listed above fail before broader checks.
- CI prints positive source, contract, layer-member, analyzed-file, dependency,
  kept-contract, and broken-contract counts without credentials or services.
- The repository owns no Python import parser or graph. The wrapper owns
  configuration integrity, the source-count canary, the AST preflights, and
  invocation of the maintained tool.
- Deleting source below the reviewed floor requires remeasurement even when the
  deletion is intentional; this is the explicit cost of detecting a silently
  empty or unexpectedly shrunken scan.

## Supersedes

None.
