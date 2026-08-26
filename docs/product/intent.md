# Product intent

Audience: humans and agents deciding what Atelier 2 should become.

The product-status sections own implementation status: what Atelier 2 currently is, proven by
what has landed. It does not own why the atelier exists — that reading is
[VISION.md](../VISION.md), and behind it
[GitHub Issue #1](https://github.com/FlexOr2/atelier-2/issues/1). The intent
section below is a derived view of those sources, kept only so these sections still
read as one piece; where it and a source disagree the source is right and
this view is simply stale. Technical decisions and implementation evidence
belong to their own owners.

Atelier 2 is intended to become a lean, independent agentic orchestrator. An
operator will describe work as versioned state machines assembled from
configurable nodes, then start, observe, steer, approve, cancel, and resume runs
through a responsive cockpit.

The intended product will:

- keep projects isolated while one operator manages several of them;
- bind each run to immutable workflow, requirement, and context revisions so a
  restart can continue from confirmed checkpoints without silently changing its
  instructions;
- expose each node's provider, model, capabilities, tools, skills, permissions,
  budget, inputs, outputs, retry behavior, cancellation, and transition;
- give Claude and Codex the same product and capability contract through thin
  provider boundaries;
- leave issues, pull requests, checks, reviews, merges, and history with the
  external development platform, while the core owns only its product concepts;
- treat every agent node as the same kind — a code node and a review node
  differ only by what the workflow gives and allows and by the agent Markdown;
  a successor sees declared outputs, not the predecessor's workspace;
- land a changed tree only through the platform adapter (an Action node, or
  the same effect as a grant on an Agent), with the secret never in the lease
  and no ambient CI credential; and
- show which source, context, workflow, proof, landed object, and deployment a
  visible result represents instead of inventing a second truth.

V1 is intended for one operator, on infrastructure they control, across multiple
isolated projects. GitHub is the first product path behind a replaceable platform
boundary; native CI, review, and squash merges remain authoritative there. A
future platform adapter may support GitLab without changing the core contract.

V1 does not include multi-user SaaS, a public shell, a home-grown replacement
for Git, pull requests, or CI, a package manager, or speculative knowledge-graph,
vector-store, security, or extension architecture.

