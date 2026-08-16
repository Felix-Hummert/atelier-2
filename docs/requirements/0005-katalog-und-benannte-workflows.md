# Requirement 0005: Named lineages, not hashes, are what the operator picks

```text
Status:         DRAFT
Owner-Issue:    https://github.com/FlexOr2/atelier-2/issues/22
Source-Threads: #22, #6, #8, #63
Distilled-From: 5301973340
                #22 body, sha256
                9cf109a2f2915116a8c32f6d74b46579a96513932ca2d03af1814a36cbac43e7
Approved-By:    none
```

`DRAFT`: the operator has not approved this reading. [ADR
0007](../decisions/0007-catalog-identity.md) is ACCEPTED and owns catalog
identity; this file does not restate its store decisions. It names the
operator-facing sentences that reading still owes, and it points at the record
for how they are built. **Exactly one rule below is graded `OPERATOR`** — rule
1, on the sentence 5301973340 quotes from him. Everything else is `DESK` and
binds nothing until he rules it.

## Intent

The operator wants to decide which agents and skills he has and where they come
from. Sharing them is a later requirement. (5301973340.)

Named, versioned, proven chains — not a pile of revision hashes — are what new
work is laid back into. (#6; ADR 0007 Context.)

## Rules

### REQ-KATALOG-01: Der Operator bestimmt, welche Agenten und Skills er hat und woher sie kommen.
Status:     DRAFT
Quelle:     OPERATOR — 5301973340, quoting him: „Ich würde gerne bestimmen können, welche Skills und Agenten ich habe und wo sie herkommen"
Begründung: That is the only operator sentence this subject carries. How sources are registered, and that git files are the import truth, is desk and lives on ADR 0007 / #22, not in this grade.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Sharing of agents, skills and whole workflows is a future requirement (Eigentümer: Operator / #22, Ziel: not this document)

### REQ-KATALOG-02: Was der Operator wählt, ist eine Lineage, nicht ein Revisions-Hash.
Status:     DRAFT
Quelle:     DESK — [ADR 0007](../decisions/0007-catalog-identity.md) Decision 1
Begründung: A hash is the revision's identity. The thing that keeps a name across republish is the lineage the record defines. This sentence is the operator-facing consequence; the derivation of the id is the record's.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Landed only when #63 cuts over `catalog_lineages` (Eigentümer: #63, Ziel: Bahn B)

### REQ-KATALOG-03: Ein Anzeigename aus genau 64 hexadezimalen Kleinbuchstaben wird beim Namen verweigert.
Status:     DRAFT
Quelle:     DESK — [ADR 0007](../decisions/0007-catalog-identity.md) Decision 1
Begründung: Otherwise a typed name and a lineage id cannot be told apart. The refusal is at the name, not after a second discriminator.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Same owner as REQ-KATALOG-02 (Eigentümer: #63, Ziel: Bahn B)

### REQ-KATALOG-04: Name und Beschreibung einer aufgenommenen Revision kommen aus den authored Bytes, oder sie fehlen ehrlich.
Status:     DRAFT
Quelle:     DESK — [ADR 0007](../decisions/0007-catalog-identity.md) Decision 1 and 4
Begründung: The catalog must not invent a description. A V3 document or agent definition already carries the fields; a V1/V2 revision does not, and the picker must show absent rather than empty.
Journeys:
Beweis:     a-published-revision-is-listed-with-the-name-its-author-wrote
Offen:

### REQ-KATALOG-05: Eine nur publizierte Revision ist noch kein wählbares Katalogmitglied.
Status:     DRAFT
Quelle:     DESK — [ADR 0007](../decisions/0007-catalog-identity.md) Decision 3
Begründung: Publication and admission are two acts. The operator must not be offered a name that the catalog has not admitted.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Admission command and picker membership wait on #63 (Eigentümer: #63, Ziel: Bahn B)

## Open questions

- **When does a source change become a new admitted revision?** ADR 0007 Decision 2 names the question; #22 still owns the operator-visible slice. (Eigentümer: #22, Ziel: after the store cutover.)
- **Sharing** is named by the operator as future and is not designed here. (Eigentümer: Operator, Ziel: later requirement issue.)

## Acceptance

`a-published-revision-is-listed-with-the-name-its-author-wrote` is declared in
`acceptance/22-where-named-workflows-live.toml` and is the only bound `Beweis`
above. REQ-KATALOG-01, 02, 03 and 05 are `UNGEBUNDEN`.
