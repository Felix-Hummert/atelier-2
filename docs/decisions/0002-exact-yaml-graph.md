# ADR 0002: Exact safe-YAML revisions own versioned graph execution

- Status: accepted for the first graph slice
- Date: 2026-08-10
- Depends on: [ADR 0001](0001-durable-runtime.md)

## Context

Atelier needs a workflow definition that is human-readable, immutable for a
running execution, independent of a provider, and precise enough to resume at a
confirmed node after a crash. YAML syntax alone does not supply graph validity,
safe loading, stable identities, transition ownership, or proof that a retry is
the same operation.

## Decision

The exact UTF-8 bytes of one YAML document identify a `WorkflowRevision` by
SHA-256. Parsing uses PyYAML's safe loader behind Atelier's adapter and validates
a strict, frozen Pydantic contract before the revision or run is written. V1
and V2 accept one acyclic, fully reachable chain with these configured node
kinds:

- `agent`: configured job and nonempty output, then one successor;
- `action`: at most one, immediately preceded by one Agent, then one successor;
- `wait`: one canonical base-10 integer answer (including rejecting `-0`), then
  one successor; and
- `subworkflow`: the single terminal node, adding exactly two strict integers.

The versions differ only at the Agent contract. V1's Agent carries an exact
expected output. V2's Agent carries a role and job; its run-start command must
provide exactly one immutable agent-configuration revision for every graph role
and no others. The role matrix is ordered by exact UTF-8 bytes, hashed, and
persisted with the run. A retry may not change it. Auth profiles contain public
selection metadata only; credentials and provider handles are neither workflow
nor durable-state fields.

Node list order has no execution meaning. The configured `start` and `next`
edges own execution order. Unknown fields and node kinds, duplicate keys or
identifiers, missing references, cycles, unreachable nodes, multiple documents,
BOMs, anchors, aliases, merges, and explicit tags are rejected before any
product mutation.

Each node execution identifier binds run, revision, and node through Atelier's
length-framed hash preimage. A successful transition atomically advances the
run's state version and event cursor with one immutable event; `WAITING_INPUT`
and reconciliation transitions deliberately retain the current node. An
`ACTION_RECONCILIATION_REQUIRED` event records the durable reason without a
receipt, while `ACTION_RECONCILIATION_RESOLVED` and `ACTION_COMPLETED` bind the
exact receipt. A terminal hash covers the ordered event hashes. Wait answers
have their own deterministic workflow identity; identical concurrent
submissions converge, while a different answer loses with a typed conflict.

[ADR 0001](0001-durable-runtime.md) remains the owner of DBOS execution,
external-effect reconciliation, crash recovery, and the canonical store. This
record owns only the document, graph, identity, and transition contracts.

## Consequences

The graph can be extended only by changing this versioned contract and adding
behavioral and crash evidence; permissive parsing or provider-specific node
meaning cannot leak into the core. Neither implemented version is a general
graph language: they have no branching, loops, arbitrary Action count,
free-form answer types, or user-selected subworkflow operation.

Pydantic and PyYAML are direct runtime dependencies because they own strict
closed-shape validation and maintained safe YAML parsing. Atelier still owns
the domain invariants, byte identities, durable transitions, and rejection
semantics.

## Executable evidence

| Proof | What it establishes |
| --- | --- |
| Domain and YAML tests | Closed safe syntax, frozen collections, graph invariants, and literal identity/hash vectors. |
| Schema and start tests | Exact V3 shape, immutable composite bindings, parse-before-write, and idempotent exact run start. |
| Runtime graph tests | YAML edges alone drive Agent, Action, Wait, and Subworkflow; event order, terminal hash, reconciliation visibility, shared continuation, and concurrent answers are durable. |
| Crash graph tests | Answer commit, Action continuation, and successor scheduling resume after process death without duplicate transitions or a changed terminal result. |
| V2 binding tests | Exact role/configuration coverage is required before write; the matrix survives restart, provider dispatch, commit-gap process death, and public projection without rebinding. |

The repository gate is `.github/workflows/ci.yml`; the local crash lane is
`uv run --locked pytest -n auto tests/crash`.
