# Requirement 0007: A run exports an evidence dossier whose limits are explicit

```text
Status:         DRAFT
Owner-Issue:    https://github.com/FlexOr2/atelier-2/issues/104
Source-Threads: #104, #1, #110
Distilled-From: #104 body, sha256 over the exact served bytes — 1931 bytes —
                1ed6ac7ac33c002c3817fb6edfb08fac68a0f7a0959bb1c770021c08ebcc5aa2
                #104 amendment 5302554371; #104 body correction 5308057631;
                #1 comment 5302467665, point 1;
                #110 body, sha256 over the exact served bytes — 9312 bytes —
                ecc1ef5593508e01538e8006eca9e9f2240739ccdc76670014293454c8949466
Approved-By:    none
```

`DRAFT`: the operator has not approved this reading. Issue #104 is the editable
human requirement and wins immediately if this view differs from it. This
document records the desired dossier and its proof boundary; it makes no claim
that the capability is implemented today.

The body digests are over the exact served bytes, reproduced with

```console
$ gh api repos/FlexOr2/atelier-2/issues/104 --template '{{.body}}' | sha256sum
$ gh api repos/FlexOr2/atelier-2/issues/110 --template '{{.body}}' | sha256sum
```

## Intent

The operator's complete sentence is:

> Das ist eine echte Marktlücke — mit aufnehmen.

That sentence decides only that the evidence dossier belongs in the product and
that its value is real. The shape below is the desk's reading of how to carry
that outcome without inventing another envelope or claiming evidence that does
not exist.

Issue [#104](https://github.com/FlexOr2/atelier-2/issues/104) owns the dossier
outcome and its later decision questions. Issue
[#110](https://github.com/FlexOr2/atelier-2/issues/110) owns the receipt-in-event
chain until that invariant is demonstrably retired. [ADR
0006](../decisions/0006-node-vocabulary.md) and Issue
[#194](https://github.com/FlexOr2/atelier-2/issues/194) own V3 context, requests,
receipts, and execution. [ADR 0007](../decisions/0007-catalog-identity.md) and
Issue [#63](https://github.com/FlexOr2/atelier-2/issues/63) own catalog identity,
lineage, and named start. [ADR
0012](../decisions/0012-acceptance-trace-format.md) owns acceptance tracing, not dossier
export. None of those mechanisms is repeated here.

## Rules

### REQ-DOSSIER-01: Atelier 2 nimmt ein Beweis-Dossier für Runs und Features als Produktfähigkeit auf.
Status:     DRAFT
Quelle:     OPERATOR — #104 body @ 1ed6ac7a, wörtlich: „Das ist eine echte Marktlücke — mit aufnehmen.“
Begründung: The quote carries product adoption and value only. The dossier's envelope, verification method, and proof boundary are desk decisions below rather than words attributed to the operator.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-DOSSIER-02: Das Dossier wird als in-toto Statement v1 im DSSE-Umschlag emittiert und offline mit cosign plus mitgeliefertem Trust-Root verifiziert; Eigenformat und Selbstbau-Verifier werden nicht verwendet.
Status:     DRAFT
Quelle:     DESK — #104 amendment 5302554371 R1 und #104 body @ 1ed6ac7a
Begründung: The accepted standard envelope and verifier are more credible to an external auditor than a private format whose verifier the product also authored.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-DOSSIER-03: Das Dossier kennzeichnet jede enthaltene Aussage als bewiesen oder unbewiesen und behauptet nur die Kette, deren Urbilder und Bindungen offline nachrechenbar sind.
Status:     DRAFT
Quelle:     DESK — #104 amendment 5302554371 R2 und #110 body @ ecc1ef55
Begründung: A signed envelope must not make an unverified assertion look equivalent to a recomputable one. The dossier names its own proof boundary instead of silently extending the receipt chain.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

## Open questions

These are the only requirement-level questions left here. Each is owned by
Issue #104 and targets the dossier ADR after its named prerequisites:

- propose the predicate upstream at in-toto issue #554, or publish one under the
  in-toto new-predicate guidelines (Eigentümer: #104, Ziel: Dossier-ADR nach den
  benannten Vorbedingungen);
- consume Chainloop, or identify the measured gap that justifies a separate
  evidence store (Eigentümer: #104, Ziel: Dossier-ADR nach den benannten
  Vorbedingungen);
- choose a redaction and omission-proof model that does not disguise missing
  material (Eigentümer: #104, Ziel: Dossier-ADR nach den benannten
  Vorbedingungen); and
- decide whether Rekor or SCITT is an additional anchor, or no additional
  anchor is needed (Eigentümer: #104, Ziel: Dossier-ADR nach den benannten
  Vorbedingungen).

## Acceptance

`REQ-DOSSIER-01`, `REQ-DOSSIER-02`, and `REQ-DOSSIER-03` are all `UNGEBUNDEN`:
no story has declared any of them. There is no `acceptance/104-*` or
`acceptance/110-*` declaration. The H1a foundation in PR #198 and the
implementation evidence in PR #199 belong to Issue #194; neither is silently
relabeled as proof of a dossier sentence.
