# ADR 0013: A bounded `iterate` block remains authored and unexecutable

- Status: structurally SUPERSEDED 2026-08-18 by
  [ADR 0014](0014-in-graph-rounds.md) and #449. The document surface remains
  implemented and readable; #449 withdrew its former child binding and execution
  claims. The real starter refuses a `subworkflow` node before a run,
  configuration or enqueue write.
- Date: 2026-08-16; amended 2026-08-21 to retain only the authored surface after
  #449 withdrew the tests-only binding, and 2026-09-01 after #450 tore out the
  composed preview this record's author surface reads before a start.
- Requirement authority: [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1)
- Decision authority: [Issue #25](https://github.com/FlexOr2/atelier-2/issues/25)
- Depends on: [ADR 0002](0002-exact-yaml-graph.md) and
  [ADR 0006](0006-node-vocabulary.md)

## Retained author surface

`iterate` is an optional closed field of a `subworkflow` node:

```yaml
- id: build_review_fix
  type: subworkflow
  workflow: {ref: build_review_fix_round, revision: "<workflow revision id>"}
  iterate:
    maximum_rounds: 4
    until:
      output: verdict
      schema: {ref: review_verdict, revision: "<schema revision id>"}
    carry:
      - name: previous_verdict
        from_output: verdict
        seed: {node: plan, output: brief}
```

The parser preserves the bound, green condition and handover exactly as authored.
It refuses a missing, non-positive or out-of-range bound; a green condition that
contradicts the node's own output schema; duplicate handovers; and a seed the
document cannot order or declare. `iterate` on another node kind is refused rather
than ignored. The composed preview names the bound and condition before any start.

These document-level checks do not bind a referenced workflow or make a run
executable. That boundary is intentionally absent after #449.

**2026-09-01 amendment (Operator-Ruling 2026-08-30, [#450](https://github.com/FlexOr2/atelier-2/issues/450)):
no surface names the bound and condition before a start.** The composed preview
the paragraph above relies on was torn out as an unbuilt derivation.
`application/compose_preview.py`, `contracts/composed_preview_v3.py` and
`contracts/capabilities_v3.py` never reached an entry point of this build: they
lived through domain tests alone from #83 until their deletion, so the surface
that was to name a bound and its green condition was never one an operator could
open. The authored half this record retains is untouched — the parser still
preserves and refuses exactly as decided above. What fell with the derivation is
only its reading before a start, and `acceptance/25` retires
`the-preview-names-every-iteration-with-its-bound-and-its-green` under the same
ruling. **The first surface that draws a revision before it runs owns that
conclusion afresh**, built from the living start owners rather than from this
record's assumption that a preview is already there to extend.

## Superseding record

ADR 0014 remains the record that superseded this ADR's former in-graph identity
conclusion. Its distinct loop surface is not evidence that this authored form is
executable.

## Supersedes

None.
