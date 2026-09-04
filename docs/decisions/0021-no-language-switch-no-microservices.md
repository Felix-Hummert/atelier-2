# ADR 0021: No language switch, no microservices, no event-sourcing core, no Temporal — the measured cost is the shape of a change, not the stack

- Status: ACCEPTED 2026-09-04 — a decision against four structural moves;
  nothing to build
- Date: 2026-09-04
- Decision authority: the operator ruling of 2026-09-04 on proposal
  [#1194](https://github.com/FlexOr2/atelier-2/issues/1194), which owns the
  proposal history. Two independent counter-checks are recorded there — the
  first rejected the draft, the second accepted it with changes.
- Amends: [ADR 0001](0001-durable-runtime.md) — Temporal is no longer unscored
  and outside the operating model; it is evaluated, rejected, and carries a
  resumption trigger
- Neighbours: [ADR 0005](0005-enforced-package-boundaries.md) (the boundaries
  a single process still enforces), [ADR 0009](0009-runner-trust.md) (the one
  isolation boundary this record does not touch),
  [ADR 0020](0020-provider-boundary.md) (the provider seam, decided separately)

## Context

The operator's question of 2026-09-04 was how to work more effectively now that
this code is read and written by agents: optimal is what lets an agent with
little context act correctly. A language change, a service split, an
event-sourced core and an external workflow engine were the large structural
candidates for that answer, and proposal #1194 measured whether any of them
addresses the observed cost.

The evidence collected there points elsewhere:

- Agent success is a function of change size: about 48 % for one file and fewer
  than five changed lines, below 10 % from three files or a hundred lines, and
  zero from seven files (arXiv:2505.23419v2). This repository's last sixty
  first-parent merges averaged 11.2 changed files, median 5; excluding tests
  and generated files, 6.9 files and 291 lines per merge.
- What helps is machine-readable feedback, not more prose: a machine-readable
  error channel raised repair success by 44 percentage points
  (arXiv:2607.14167), a reproduction test is the strongest available oracle
  (ORACLE-SWE 2026), while static context files did not raise success and cost
  over a fifth of the context window (CTXBENCH 2026). Code written by agents
  duplicates more and hides defects more often (GitClear 06.2026,
  correlational).
- Finding the right place is not the bottleneck: 98 % of the symbols in
  `src/atelier2` are unique, and plain search resolves them.

None of those measurements names the language, the process topology, or the
persistence style as the cost. All of them name the size and shape of a single
change and the quality of the feedback it gets. The installation this decision
serves is one live instance with one user, whose durable execution DBOS already
owns behind one adapter (ADR 0001).

## Decision

Four structural moves are rejected. Each names the one trigger that reopens it,
and only that trigger; reopening is a fresh proposal with its own counter-check,
not a judgement call inside a slice.

1. **No language switch.** Python owns the core and TypeScript owns the
   cockpit. Trigger: a *measured* agent gain of another language for this kind
   of work — a measurement against this repository's own tasks, not a general
   benchmark.
2. **No microservices.** The product stays one deployable service with its
   enforced package boundaries (ADR 0005); a boundary is a package and a port,
   not a network hop. Trigger: independent scaling of one part, or more than
   one user or repository. The isolation boundary of ADR 0009 is a separate
   question and is not decided away here.
3. **No event-sourcing core.** Durable truth stays state in tables with
   append-only ledgers beside it, not a log that state is replayed from.
   Trigger: more than one user or repository, where reconstructing history per
   tenant becomes a product requirement rather than an audit convenience.
4. **No Temporal.** DBOS owns durable execution in the same process and against
   the same canonical store; Temporal adds a separate server, a worker fleet
   and a second state store, which is a second operating surface for a single
   live instance and no measured gain against the cost the evidence names.
   Trigger: independent scaling, or more than one user or repository.

This is an architecture decision, not an agent rule. AGENTS.md carries no
sentence about these four moves, because a contract line an agent must remember
is the wrong owner for a question that is asked once and answered here.

## Consequences

- Nothing in the tree changes on this record. Its effect is that the effort
  goes into the ruled code rules instead: the slice corridor, the debt
  ratchets, the machine-readable checks, and the audit that works them off.
- A slice that would need one of the four moves is cut differently or brought
  back as a proposal against this record's trigger. "It would be cleaner in
  another language" is not a trigger; a measurement is.
- The accepted risk is that a trigger arrives quietly — several repositories or
  a second user creeping in through a feature rather than a ruling. The audit
  that reviews the tree against the code rules names such drift, which is why
  the triggers are stated as observable facts and not as feelings.

## Amends ADR 0001

ADR 0001 recorded that "Temporal is not scored or rejected by the probe; it is
outside the V1 single-process operating model." As of 2026-09-04 that sentence
no longer holds: Temporal is scored and rejected, with the resumption trigger
above. ADR 0001's own decision — DBOS behind the `src/atelier2/adapters/dbos/`
boundary, one canonical store — stands unchanged, and the amendment is noted
beside the passage it changes.

## Out of scope

This record decides nothing about the provider boundary (ADR 0020), about
isolation for foreign repositories or multiple users (ADR 0009), about the
frontend framework, or about which libraries the existing stack uses.
