# Requirement 0006: Atelier 2 takes over its own work under proof

```text
Status:         DRAFT
Owner-Issue:    https://github.com/FlexOr2/atelier-2/issues/1
Source-Threads: #1
Distilled-From: #1 body, sha256 over the exact served bytes — 25489 bytes —
                ca28be560c82ca7f8b56df52d4914db3cd0a82efb3f50e740604b6e3065d5dc0
Approved-By:    none
```

`DRAFT`: the operator has not approved this reading. Issue #1 is the editable
human requirement and wins immediately if this view differs from it. This
document says what controlled self-adoption must achieve; it makes no claim
about how much of it is implemented today.

The body digest is over the exact served bytes, reproduced with

```console
$ gh api repos/FlexOr2/atelier-2/issues/1 --template '{{.body}}' | sha256sum
```

## Intent

The operator's complete sentence is:

> **Kontrollierte Selbstübernahme.** Atelier 2 führt seinen eigenen Workflow im
> Shadow-/Canary-Modus aus und ersetzt den Bootstrap-Harness erst nach Parität,
> Restart-/Exactly-once-Beweis, Rollback und Deletion Ledger.

This document owns only that outcome and its replacement gate. Requirement
[0004](0004-runner-und-remote.md) and [ADR
0009](../decisions/0009-runner-trust.md) own runner trust; [ADR
0006](../decisions/0006-node-vocabulary.md) owns the V3 authoring language and
node records; [ADR 0007](../decisions/0007-catalog-identity.md) and
[Issue #63](https://github.com/FlexOr2/atelier-2/issues/63) own catalog identity
and named start. Issues [#15](https://github.com/FlexOr2/atelier-2/issues/15),
[#60](https://github.com/FlexOr2/atelier-2/issues/60), and
[#194](https://github.com/FlexOr2/atelier-2/issues/194) own implementation
slices. None of their mechanism is repeated here.

## Rules

### REQ-SELBSTBAU-01: Atelier 2 führt seinen eigenen Workflow im Shadow-/Canary-Modus aus.
Status:     DRAFT
Quelle:     OPERATOR — #1 body @ ca28be56, wörtlich: „Atelier 2 führt seinen eigenen Workflow im Shadow-/Canary-Modus aus"
Begründung: Self-adoption is a product capability, not a special execution path. The sentence requires the product to exercise its own published workflow while the external harness still provides a safe comparison.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-SELBSTBAU-02: Atelier 2 ersetzt den Bootstrap-Harness erst nach Parität, Restart-/Exactly-once-Beweis, Rollback und Deletion Ledger.
Status:     DRAFT
Quelle:     OPERATOR — #1 body @ ca28be56, wörtlich: „Atelier 2 führt seinen eigenen Workflow im Shadow-/Canary-Modus aus und ersetzt den Bootstrap-Harness erst nach Parität, Restart-/Exactly-once-Beweis, Rollback und Deletion Ledger."
Begründung: Removing the external harness is a separate act from running a canary. The named proofs keep that irreversible cutover behind evidence rather than confidence.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

## Open questions

These sentences leave no requirement-level question open. Their missing proof
is named under Acceptance rather than turned into design here.

## Acceptance

`REQ-SELBSTBAU-01` and `REQ-SELBSTBAU-02` are both `UNGEBUNDEN`: no story has
declared either sentence. Issue #60 describes a foundation canary and explicitly
does not claim general self-build. Likewise,
[`acceptance/194-a-v3-agent-document-starts.toml`](../../acceptance/194-a-v3-agent-document-starts.toml)
declares a private one-node foundation and explicitly disclaims provider
invocation, a V3 receipt, and terminal completion. Neither is silently relabelled
as proof of these requirements.
