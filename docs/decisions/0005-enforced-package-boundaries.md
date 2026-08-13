# ADR 0005: CI enforces package boundaries

## Context

Atelier 2 has distinct host, API, adapter, application, port, and contract
owners, but prose and review alone cannot prevent imports from reversing those
dependencies. A boundary gate must catch real import paths, unexpected new
top-level packages, and ownership of DBOS and SQLAlchemy without replacing a
maintained import-analysis tool with project code. It must also fail loudly if
configuration disappears or a source scan unexpectedly shrinks.

Import Linter 2.13 and Tach 0.35.0 were measured on the same source tree. Import
Linter analyzed 84 files and 454 dependencies, rejected the tested inward,
external-owner, and empty-package violations, and added five locked packages.
Tach omitted seven import-free modules from its map, accepted the empty-package
violation, and added fourteen packages. Import Linter therefore covers the
required behavior with the smaller dependency cost. Its own scan still remains
green after a deleted leaf, so a narrow repository preflight owns the positive
source floor and exact reviewed contract metadata.

## Decision

`pyproject.toml` is the executable owner of three Import Linter contracts. The
layer contract is exhaustive, treats API and adapters as peers, and permits
dependencies only toward the right of this derived view:

<!-- architecture-contract-view:start -->
```text
layers: __main__ > host > api | adapters > application > ports > contracts
dbos-owner: atelier2.adapters.dbos
root-facade-forbids: __main__, host, api, adapters, application, ports
```
<!-- architecture-contract-view:end -->

The root facade cannot bypass ports through a package or descendant import.
Only `atelier2.adapters.dbos` may import DBOS or SQLAlchemy. The derived view
above is checked byte-for-byte against a deterministic rendering of the
executable configuration; it is not an independent policy owner.

The quality lane runs `uv run --locked python scripts/check_architecture.py`
immediately after dependency installation. The preflight requires at least the
reviewed 56 production modules, exactly three named contracts, and exactly the
seven declared layer members. It then delegates import analysis and reporting
to Import Linter with caching disabled and timings visible. Growth is allowed;
shrinkage below the measured floor requires a deliberate remeasurement and
review of this decision and the executable floor.

## Consequences

- Forbidden inward imports, root-facade bypasses, DBOS/SQLAlchemy ownership
  violations, and undeclared top-level packages fail before broader checks.
- CI prints positive source, contract, layer-member, analyzed-file, dependency,
  kept-contract, and broken-contract counts without credentials or services.
- The repository owns no Python import parser or graph. The wrapper owns only
  configuration integrity, a source-count canary, and invocation of the
  maintained tool.
- Deleting source below the reviewed floor requires remeasurement even when the
  deletion is intentional; this is the explicit cost of detecting a silently
  empty or unexpectedly shrunken scan.

## Supersedes

None.
