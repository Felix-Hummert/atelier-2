from __future__ import annotations

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
