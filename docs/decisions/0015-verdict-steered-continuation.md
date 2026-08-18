# ADR 0015: A declared verdict steers the loop's back edge, under a contract this product owns

- Status: ACCEPTED 2026-08-19 — the document form, the closed vocabulary, its
  published contract and the verdict-driven continuation are implemented; the
  agent's own named refusal is not, and "Named and not built" says why
- Amends: [ADR 0014](0014-in-graph-rounds.md), which left the result-driven end of
  a loop explicitly undecided; the bound it made mandatory stays mandatory
- Refines: [ADR 0006](0006-node-vocabulary.md) — "the format expresses no
  conditional branching" holds for `depends_on`, which stays a list of node ids;
  the one conditional edge this record admits is the loop's own back edge
- Requirement authority: [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1)
- Decision authority: [Issue #25](https://github.com/FlexOr2/atelier-2/issues/25),
  the operator ruling of 19.08. at
  [comment 5318394119](https://github.com/FlexOr2/atelier-2/issues/25#issuecomment-5318394119)
  ("a review REVISE is not a failure; its typed output decides the continuation"),
  and the fractal ruling of [#361](https://github.com/FlexOr2/atelier-2/issues/361)
  — one owner for the vocabulary, not one per edge
- Depends on: [ADR 0002](0002-exact-yaml-graph.md) (the acyclic graph),
  [ADR 0014](0014-in-graph-rounds.md) (the loop and its rounds), the output schema
  seam of [#295](https://github.com/FlexOr2/atelier-2/issues/295)

## Context

Until now, everything a node produced flowed *forwards*: a value another node
reads, an artifact an operator looks at. Nothing a run produced could change
where the run went. That is why a loop had exactly one exit — its declared bound
— and why the workshop's real cycle could be repeated but never *finished*: a
review that says "this is done" had no way to say it to the engine.

Making an output steer an edge is the point where a product either grows a
contract or grows a guess. The guess is text matching: read the agent's prose,
look for a word, branch on it. That is unrepeatable, unversioned, and silently
wrong the first time an agent phrases itself differently.

## Decision

### The condition is declared where the conditional edge is

The only edge in this build whose target is not fixed by the document is the
loop's back edge, so the condition is declared on the loop:

```yaml
loops:
  - id: until_reviewed
    body: [implement, review]
    maximum_rounds: 3
    repeat_while: {node: review, verdict: revise}
```

`depends_on` stays a list of node ids. Turning it into a union of ids and typed
edges would rewrite every reader of the order — the body rule, the dependency
closure, the entry set, the linear successor, the published grammar — to express
nothing this build can honour. When a second conditional edge genuinely exists
(a merge node, a fan-out), the edge form is the honest place for it, and this
record deliberately leaves that door unpainted rather than guessing its shape.

**The bound stays mandatory and stays the fallback.** A verdict is the earlier
exit, never a way past `maximum_rounds`: the agent producing the verdict is the
one that would otherwise keep the loop alive forever.

### The verdict is a closed vocabulary with one owner

`accepted` and `revise`, in `contracts/verdicts.py`, and nothing else. The same
words are meant to answer for a node today and for a whole run later, which is
why they are one owner rather than one enum per edge that reads them.

A `revise` verdict is a **successful** node. Nothing failed: the reviewer did its
work and its work says "again". Conflating that with a failure is what would make
the loop unusable, because a failed node ends the run.

### The contract an answer says its verdict under is published, and the document pins it

The vocabulary generates a JSON Schema, published as an ordinary schema revision
and therefore addressed by the hash of its own bytes. A loop that reads a node's
verdict requires that node's one declared output to pin **that** revision, and a
document that pins anything else is refused by name before it can be stored.

That one rule is what makes reading a verdict *total*, and it is why this record
needs no new failure ending of its own:

- an answer is judged by the schema its author pinned before anything reads it —
  the seam every V3 output has passed since #295;
- for a deciding node, that schema **is** the verdict contract;
- so an answer that carries no verdict dies exactly where a bad output has always
  died, in the words of the schema owner, and an answer that survives carries a
  word the engine owns.

Reaching the reader with unreadable bytes would mean the stored schema and the
vocabulary had come apart; it raises rather than choosing an edge.

### Where the decision is made, and from what

`completion_after_node` — the one advancement rule every format already asks —
takes the verdict and decides. It stays a pure decision about the document: the
verdict travels *in*, because reading it is a durable act.

Both callers read the same value. The success write reads the answer in hand; a
driver recovering after a round already succeeded reads the value that round kept
as `node-artifact/v3`. A loop that declares a verdict and is asked without one
refuses by name rather than continuing in whichever direction the omission
implies — silently taking another round is the expensive direction.

## Named and not built

**The agent's own refusal.** The operator's ruling asks for a second door: the
agent may answer "the order is unclear because X" instead of producing work that
dies at the schema seam — the same receipt truth, an honest sender. The receipt
side of it costs nothing; `node-receipt/v3` reasons are an open family. What it
costs is the *ending*: a run ends `FAILED` only through an attempt failure code,
and that column's value list is a closed store contract mirrored in the published
schema shapes, the migration ladder, the wire and the frontend. A refusal written
under either existing code would tell an operator a schema refused something no
schema saw, or that a process died that exited cleanly. So the door waits for a
store hop of its own rather than landing as a false durable sentence, and the
vocabulary here carries only the words the machine can honour.

**A verdict beside other work.** A deciding node's answer is exactly its verdict
today, because a value handed *out* of a loop still names no round (ADR 0014) —
so a reviewer's findings have no reader downstream yet. When the handover lands,
a richer answer form is a revision of the published contract, which is what
revisions are for.

## Consequences

- The published grammar grows `repeat_while` and the verdict vocabulary from the
  same models the parser reads, so a publisher sees the early exit and the closed
  word list from the door.
- Two new document refusals, both decided before anything is stored:
  `loop_verdict_node_not_the_round_end` and `loop_verdict_unreadable`.
- No store hop. The verdict travels inside a value the store already keeps, and
  every durable record involved is one that already existed.
- A run whose loop is steered still ends through the terminal path every other
  run ends by; no terminal state and no failure word were minted.
- The deciding node's schema is the product's, not the author's. An instance must
  publish that revision before such a run can be judged, exactly as it publishes
  every other schema a document pins.
