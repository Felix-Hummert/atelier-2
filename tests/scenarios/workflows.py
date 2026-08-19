from __future__ import annotations

import json

from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.verdicts import (
    VERDICT_ANSWER_SCHEMA,
    VERDICT_FIELD,
    Verdict,
)

ANY_JSON_SCHEMA = PublishedRevision(RevisionKind.SCHEMA, b"true")
"""The narrowest honest contract for a node whose subject is not its schema.

A run only executes an agent node that declares one output, so every scenario
driving a V3 run has to pin some schema. Where the test is about something else
-- the arm, the order, the redeemed grant -- pinning a schema that admits every
JSON value keeps the node's contract to what the shape requires: the answer is
one JSON value, and nothing beyond that is claimed.
"""


def declared_output(
    schema: PublishedRevision = ANY_JSON_SCHEMA, name: str = "result"
) -> bytes:
    """The one authored output an executable V3 agent node carries."""
    return f"""    outputs:
      - name: {name}
        schema:
          ref: {name}-schema
          revision: {schema.revision_hash.value}
""".encode()


V3_DOCUMENT = b"""format_version: 3
name: Implement a candidate, then review it for defects
graph_outputs:
  - name: verdict
    from: {node: review, output: findings}
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Implement every acceptance sentence of the bound story.
    outputs:
      - name: candidate
        schema: {ref: workspace_candidate, revision: schema-candidate}
  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: Name every defect with the sentence it violates.
    depends_on: [implement]
    inputs:
      - name: candidate
        from: {node: implement, output: candidate}
    outputs:
      - name: findings
        schema: {ref: review_verdict, revision: schema-verdict}
"""
V3_DOCUMENT_NAME = "Implement a candidate, then review it for defects"
V3_NODE_COUNT = 2
V3_CONTROL_EDGE_LINE = b"    depends_on: [implement]\n"


LOOPED_LINE_NODE_IDS = ("implement", "review")
LOOPED_LINE_MAXIMUM_ROUNDS = 3
LOOPED_LINE_DOCUMENT = (
    b"""format_version: 3
name: Build and review, three rounds at most
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
"""
    + declared_output()
    + b"""  - id: review
    type: agent
    role: builder
    mode: headless
    instruction: Check what the node before you did.
    depends_on: [implement]
    inputs:
      - name: candidate
        from: {node: implement, output: result}
"""
    + declared_output()
    + b"""loops:
  - id: until_reviewed
    body: [implement, review]
    maximum_rounds: 3
"""
)
"""The two-node loop every round-aware scenario drives.

It is the shape the workshop actually runs by hand -- build, then review, then
build again -- cut to the thinnest form that still proves a round: two agent
nodes, the second reading the first, and a declared bound that ends it.
"""


VERDICT_LOOP_MAXIMUM_ROUNDS = 3
VERDICT_LOOP_ACCEPTING_ROUND = 2
VERDICT_LOOP_DOCUMENT = (
    b"""format_version: 3
name: Build and review until the review says it is done
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
"""
    + declared_output()
    + b"""  - id: review
    type: agent
    role: builder
    mode: headless
    instruction: Check what the node before you did, and say whether it is done.
    depends_on: [implement]
    inputs:
      - name: candidate
        from: {node: implement, output: result}
"""
    + declared_output(VERDICT_ANSWER_SCHEMA, "verdict")
    + f"""loops:
  - id: until_reviewed
    body: [implement, review]
    maximum_rounds: {VERDICT_LOOP_MAXIMUM_ROUNDS}
    repeat_while: {{node: review, verdict: {Verdict.REVISE.value}}}
""".encode()
)
"""The same loop, with the review's own answer deciding whether it turns again.

The bound is deliberately larger than the round the review accepts in, so a run
that ends early can only have ended on the verdict -- an engine that ignored it
would run to the bound and be seen doing it.
"""


def verdict_answer(verdict: Verdict) -> bytes:
    """What an agent writes when the answer it owes is a verdict.

    The provider's side of the contract, composed the way a provider composes
    it. The tokens come from the vocabulary; the bytes are the scenario's.
    """
    return json.dumps({VERDICT_FIELD: verdict.value}, separators=(",", ":")).encode()
