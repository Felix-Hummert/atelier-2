# Atelier

![The workshop — the house you arrive in](docs/atelier.jpg)

Atelier is intended to become a lean agentic orchestrator for versioned,
configurable workflows. Why it exists is the Desk/Doku reading in
[docs/VISION.md](docs/VISION.md) of
[GitHub Issue #1](https://github.com/FlexOr2/atelier-2/issues/1); what it
must be able to do is read into
[docs/requirements/](docs/requirements/README.md); what it currently is, in
[docs/PRODUCT.md](docs/PRODUCT.md). Which documentation layer answers which
question is mapped in [docs/README.md](docs/README.md).

## Fact owners

| Durable fact | Authoritative owner |
| --- | --- |
| Which documentation layer answers which question | [docs/README.md](docs/README.md) |
| Why this atelier exists (Desk/Doku reading of Issue #1; the issue wins) | [docs/VISION.md](docs/VISION.md) |
| Implementation status | [docs/PRODUCT.md](docs/PRODUCT.md) |
| Human requirement, numbered views, revision and acceptance trace | [docs/requirements/README.md](docs/requirements/README.md) |
| Technical decisions | Records indexed by [docs/decisions/README.md](docs/decisions/README.md) |
| Reusable agent policy | [AGENTS.md](AGENTS.md); [CLAUDE.md](CLAUDE.md) only loads it for Claude |
| Foundation verification | [.github/workflows/foundation.yml](.github/workflows/foundation.yml) |
| Current code verification | [.github/workflows/ci.yml](.github/workflows/ci.yml) |
| How this installation is started and redeployed | [docs/OPERATIONS.md](docs/OPERATIONS.md) |

Use [.github/pull_request_template.md](.github/pull_request_template.md) to bind
future changes to their requirement, acceptance evidence, context, decisions,
and exact Git objects. Do not copy an owner's facts into another document.
