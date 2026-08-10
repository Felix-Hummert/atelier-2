# Atelier 2

Atelier 2 is intended to become a lean agentic orchestrator for versioned,
configurable workflows. The authoritative product intent is
[docs/PRODUCT.md](docs/PRODUCT.md).

## Fact owners

| Durable fact | Authoritative owner |
| --- | --- |
| Product intent, scope, and implementation status | [docs/PRODUCT.md](docs/PRODUCT.md) |
| Editable human requirement | [GitHub Issue #1](https://github.com/FlexOr2/atelier-2/issues/1), with revision rules in [docs/requirements/README.md](docs/requirements/README.md) |
| Technical decisions | Records indexed by [docs/decisions/README.md](docs/decisions/README.md) |
| Reusable agent policy | [AGENTS.md](AGENTS.md); [CLAUDE.md](CLAUDE.md) only loads it for Claude |
| Foundation verification | [.github/workflows/foundation.yml](.github/workflows/foundation.yml) |
| Current code verification | [.github/workflows/ci.yml](.github/workflows/ci.yml) |

Use [.github/pull_request_template.md](.github/pull_request_template.md) to bind
future changes to their requirement, acceptance evidence, context, decisions,
and exact Git objects. Do not copy an owner's facts into another document.
