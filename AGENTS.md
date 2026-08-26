This file is reusable AI policy. Keep project facts, provider glue, architecture
state, tool commands, and plans outside it.

Entry points (pointers only; the facts live there): `docs/README.md` maps the
documentation layers; `docs/PRODUCT.md` indexes implementation-status sections;
`docs/requirements/README.md` owns the requirement and trace contract;
`docs/decisions/README.md` owns technical decision records;
`.github/workflows/foundation.yml` owns parentless foundation-object proof;
`.github/workflows/ci.yml` owns current code verification; `README.md` is the
human entry point to those owners.

Repository edits use the globally installed `agent-claim` CLI. A subagent stays
within its parent head's current claimed scope; the CLI owns the protocol.

## Growth

Grow the repository only to remove a named problem, preserve a real invariant,
or enable a named capability. Reuse an existing owner before creating one.

Keep facts compact and verified. Check repository claims against files and
external claims against primary sources. Mark unknowns.

Do not duplicate guidance. Update the owner.

## Code

- Identify the owner, state, configuration, failure modes, and verification
  before editing.
- Fix the cause, not the symptom. Use temporary containment only when the risk
  is named, visible, verified, and has a removal path.
- Create a new boundary only when no existing owner can honestly own the
  decision.
- Do not add architecture, extension points, compatibility layers, or options
  without a current caller.
- Build vertically first: shapes early, surfaces thin, hardening after use. A
  capability's first slice is the thinnest honest end-to-end proof; edge cases
  are mandatory before that first run only where their absence corrupts durable
  state, loses data, or lets the system lie. Every other edge is deferred by
  naming it — an open sentence or named gap on the owning item — and waits for
  usage evidence. A review judges the slice against its declared sentences, not
  against all conceivable hardness. (Operator + coordinator ruling, 16.08.2026.)
- Match existing style unless it conflicts with this file or preserves a known
  defect.
- Use readable, fully written names. Avoid abbreviations unless established.
- Prefer typed state over loose dictionaries and string protocols.
- Put behavior with the module that owns the decision.
- Keep side effects visible at the call site.
- Keep pure logic pure when side effects are not part of the contract.
- Isolate filesystem, process, network, clock, randomness, and external
  services behind narrow boundaries.
- Use comments only for unavoidable why: external constraints, security reasons,
  protocol quirks, data-loss risks, or non-obvious tradeoffs.
- Do not add inline static-analysis suppressions. Fix the design, narrow the
  code, or change central policy with verification.

## Configuration

- Values that vary by user, project, environment, provider, deployment, or
  runtime boundary belong in configuration.
- Do not make stable internal invariants externally configurable just to avoid a
  constant.
- Secrets enter through secret or configuration channels only. Never write them
  to logs, prompts, event records, memory, documentation, fixtures, or tests.

## Value Ownership

Before adding a literal, constant, default, or fixture value, classify its owner.

- Runtime, user, provider, project, environment, and boundary-specific values
  enter through commands, configuration, environment, fakes, or explicit
  call-site parameters. Do not hardcode prompts, provider output, ids, paths,
  clocks, credentials, or user/project choices.
- Stable domain concepts, protocol tokens, persisted keys, event names,
  statuses, ids, commands, and records need one production owner, preferably a
  typed contract instead of loose strings.
- Adapter representation details stay inside the adapter boundary unless a
  caller has a real contract with them.
- Test values are local scenario data. Use builders, fixtures, or scenario
  objects instead of top-level constants blocks that mix contracts, examples,
  and adapter internals.
- Tests reuse production contracts but do not duplicate production construction,
  serialization, path, or derivation logic to calculate expected results.

## Reuse

- Reuse code only when the same domain idea, owner, and invariant are present.
- Remove duplication when duplicated code must evolve together.
- Keep short duplication when abstraction would hide meaning or couple unrelated
  concepts.
- Use maintained libraries for solved hard problems when the standard library is
  not enough.
- Do not add a dependency for one trivial helper, style alone, or speculative
  flexibility.
- Explain what complexity a new dependency removes.

## Failure

- Fail loud when durable state, user data, verification, integration, security,
  or process ownership could be corrupted.
- Fail soft only when the failure is visible, recoverable, and cannot corrupt
  important state.
- Validate external input, provider output, configuration, and filesystem state.
- Enforce privileged, irreversible, or external side-effect boundaries in code
  or tool permissions; prompts are not controls.

## Tests

- Test behavior, not implementation details.
- When no production contract exists, define the smallest owner and value
  contract before behavioral tests. Do not let tests invent domain owners,
  protocol values, or adapter details.
- After the contract exists, write the failing behavioral test first for risky
  behavior, implement the smallest behavior, then refactor under the same test.
- Add focused tests for new behavior and regression tests for bug fixes.
- Coverage is evidence, not the goal. Do not add tests that only execute lines.
- Keep tests deterministic, cheap, and safe to run in parallel.
- Whenever tests run, use the repository's configured parallel execution.
- Tests must not depend on execution order, shared mutable repository state, or
  process-global state another test can observe.
- Use temporary state or fakes for filesystem, process, network, clock,
  randomness, and external services.
- For agent or provider behavior, verify repeatability, perturbation tolerance,
  failure handling, and safety bounds; one successful transcript is not proof.
- For fixture values and expected results, follow Value Ownership.

## Documentation

- Update documentation in the same change that alters behavior, architecture,
  configuration, testing, tooling, or user-visible usage.
- Do not document imagined architecture or future plans as current facts.
- Do not maintain stale-prone inventories, file maps, line references, command
  transcripts, or implementation trivia.
- Describe durable intent, contracts, invariants, and decisions at the highest
  useful abstraction.
- Write for a named reader — human operator, human engineer, or agent — at a
  named altitude; agent docs stay compact contracts, human overviews lead with
  the mental model and prefer one accurate diagram-as-code over structure
  prose.
- Facts have one owner. A document serving another audience is a view:
  derived from the owner, naming its source, corrected by regeneration —
  never independently edited.
- Every line must be accurate and earn its keep.
- Prefer one accurate paragraph over a large stale guide.
- Delete documentation that no longer matches the code.
- Use examples only when they prevent a likely misread.
- Code examples require a real stack, imports, owner, and configuration source.

## Finish

- Read the diff before finishing.
- Remove speculative code, unused code, unused documentation, duplicated
  literals, and hidden hardcoded values.
- Report the exact verification command.
- Mention larger problems separately instead of refactoring unrelated code.

## Self-Improvement

For self-improvement, report the problem removed, proof, complexity added,
verification, and what can now be deleted or simplified. Pure growth is a
defect.
