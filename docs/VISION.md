# Why this atelier exists

**DRAFT — Operator-Segnung ausstehend**

This page is a Desk/Doku reading of
[GitHub Issue #1](https://github.com/FlexOr2/atelier-2/issues/1) (body UTF-8
26837 bytes including GitHub's trailing newline, SHA-256
`070da2570c878ee8d1e37488c715cfa7af972af7c347a4554f273ef72047b3f6`, Operator
amendment 19.08.2026). It is not a second authority and not a copy of that
issue. The issue wins. Nothing below is an operator sentence except a quotation
taken from that body.

## The reason

The operator wants a lean, independent product that runs agentic work as
published graphs — and refuses to rebuild the platforms that already exist.

> „Ich will Atelier 2 als neues, eigenständiges Produkt bauen: einen schlanken
> agentischen Orchestrator, in dem ich Abläufe als versionierte, deklarative
> Workflow-Graphen aus konfigurierbaren Nodes beschreibe. Das Produkt soll nicht
> GitHub, GitLab, CI, einen Agent-Paketmanager oder Wissensspeicher neu
> erfinden, sondern bestehende Plattformen hinter kleinen austauschbaren
> Grenzen verwenden."

Desk/Doku-Lesart of that paragraph: a run binds one immutable workflow
revision. The person starts, observes, steers, approves, cancels, and resumes
through one cockpit. The core owns Requirement, Workflow, Run, Context,
Capability, Budget, and Receipt. External platforms keep their native objects.
The cockpit projects those truths and does not invent a second set.

He keeps the workflow:

> „Der Operator behält jederzeit die fachliche Kontrolle darüber, welcher
> Workflow verwendet wird."

Desk/Doku-Lesart of that sentence: Atelier may propose or compose. It does not
add a hidden ceremony. Publish and start are separate acts. After start, the
bound revision does not move.

Coder and reviewer are the same node kind. A Markdown agent definition owns the
reusable worker identity and its stable system behaviour; a workflow Agent node
is one occurrence of that worker. The node names a portable role, a required
workflow-generic instruction, what it **gets** (typed run inputs, pinned source)
and what it **may do** (capability, tools, grants). At run start the role binds to
one exact configuration; the target binding makes the exact agent definition
behind it reconstructible. Concrete story material enters as a typed run input,
never by interpolating task bytes into the instruction. A follow-on node sees
only mapped outputs, not the predecessor's throwaway directory. Landing (push,
PR, merge) is one platform-adapter effect: an Action node, or the same effect as
a grant on an Agent. The secret never enters the lease. A CI host may hold git
and a token; the agent does not inherit them. The issue's own words:

> „Coder-Knoten und Review-Knoten sind dieselbe Node-Art (`agent`). […]
> Irreversible externe Mutationen wie Push, Merge oder Deploy laufen
> ausschließlich über Intent, Readback und Receipt des Plattform-Adapters —
> nie als Roh-`git push` im Provider-CLI-Stream, nie mit Token in der
> Agent-Lease."

## What this page does not claim

Issue #1 also names a V1 success, a *Nicht V1* exclusion list, and a dated
delivery note. The exclusions are still the issue's. The delivery note is not
this page's — [PRODUCT.md](PRODUCT.md) owns what exists today. This page does
not treat an intended cockpit, an intended catalogue, or an intended self-run
as present.

## Later visions, referenced only

These four issues are marked `VISION/PROPOSAL` on their own bodies. They become
requirements only if the operator publishes them the way
[Issue #5](https://github.com/FlexOr2/atelier-2/issues/5) already decided.
This page does not restate them.

- [Issue #6](https://github.com/FlexOr2/atelier-2/issues/6) — named, versioned
  catalogue of proven chains
- [Issue #7](https://github.com/FlexOr2/atelier-2/issues/7) — a conductor: chat
  becomes work, as the engine's first customer
- [Issue #8](https://github.com/FlexOr2/atelier-2/issues/8) — a scorecard from
  measurements, never from self-grades
- [Issue #9](https://github.com/FlexOr2/atelier-2/issues/9) — the living graph,
  ephemeral tiles, mode as a capability

## What this page is not

Not what must be built — [requirements/](requirements/). Not how a person walks
the surfaces that exist — [journeys/](journeys/). Not why a technical shape was
chosen — [decisions/](decisions/).
