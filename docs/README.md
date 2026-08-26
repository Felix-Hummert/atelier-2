# Documentation map

Audience: a human or an agent who has just opened `docs/` and needs to know
which layer answers which question.

This file is a map. It owns no product fact. Each layer has its own owner;
where this map and an owner disagree, the owner is right.

The name has three layers ([#515](https://github.com/FlexOr2/atelier-2/issues/515),
operator ruling 22.08.): the product is **atelier**, the GitHub repository
stays `atelier-2` until the operator moves it, and technical identifiers —
Python package, CLI, store/event/problem URNs, container labels — stay
`atelier2` until each is renamed as its own migration.
[`productName.ts`](../frontend/src/lib/productName.ts) owns the visible
string.

## Which question lives where

| Question | Layer | Owner today |
| --- | --- | --- |
| Why does this atelier exist? | Vision | [`VISION.md`](VISION.md). Desk/Doku reading of [Issue #1](https://github.com/FlexOr2/atelier-2/issues/1); the issue wins. Copying Issue #1 into this tree is forbidden. |
| What does the house look like? | Face | [`atelier.jpg`](atelier.jpg). One picture, one owner. The README embeds it so GitHub shows the door. Not a second logo farm. |
| What must it feel like? | Heart | [`HEART.md`](HEART.md). The design soul every hand-built surface answers to; the clarity contract says what must be true, this says what it must feel like. |
| What must it be able to do? | Requirements | [requirements/README.md](requirements/README.md) |
| How is it used? | Journeys | [journeys/README.md](journeys/README.md) |
| What does the machine count as done? | Acceptance | [requirements/README.md](requirements/README.md) |
| Why was it built this way? | Decisions | Records indexed by [decisions/README.md](decisions/README.md). |
| What exists today? | Product | [PRODUCT.md](PRODUCT.md), the implementation-status index. |
| How is this installation started? | Operations | [OPERATIONS.md](OPERATIONS.md). The operator runbook for the packaged serve. |
| How is an executor toolchain pinned? | Operations | [OPERATIONS.md](OPERATIONS.md). An atelier-owned copy; not the operator's daily `~/.local/bin` CLI. |

Agent policy lives in [`AGENTS.md`](../AGENTS.md) at the repository root, not here.

## Audit contract

[requirements/README.md](requirements/README.md) owns the audit-chain and
revision rules. [ADR 0012](decisions/0012-acceptance-trace-format.md) owns the
acceptance trace format.

## What this map does not do

It does not list every file. It does not restate the status sections indexed by
[PRODUCT.md](PRODUCT.md) or
any requirement. It does not treat a planned layer as present.
