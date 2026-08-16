# Requirement 0005: Named lineages, not hashes, are what the operator picks

```text
Status:         DRAFT
Owner-Issue:    https://github.com/FlexOr2/atelier-2/issues/22
Source-Threads: #22, #6, #63
Distilled-From: 5301973340 — the quoted operator sentence (rule 01)
                #22 body, sha256
                9cf109a2f2915116a8c32f6d74b46579a96513932ca2d03af1814a36cbac43e7
                #6 — named, versioned, proven chains (Intent)
                ADR 0007 Decisions 1, 3 and 4 and section 9 (rules 02-05)
                #63 — owner of resolver, admission and picker (Offen)
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

### REQ-KATALOG-02: Der Operator wählt eine Lineage bei ihrem Namen; durabel gebunden wird danach der exakte Revisions-Hash.
Status:     DRAFT
Quelle:     DESK — [ADR 0007](../decisions/0007-catalog-identity.md) Decision 1 und Abschnitt 9
Begründung: Zwei Dinge zu zwei Zeitpunkten, und das Trennen ist der ganze Satz. Gewählt wird, was den Namen über ein Neu-Publizieren hinweg behält — die Lineage. Aufgelöst wird der Name nach Abschnitt 9 **genau einmal, zur Autorenzeit, bevor die Run-Konfigurations-Revision publiziert wird**; danach existieren nur noch Ids und Hashes. Gebunden ist deshalb der exakte Revisions-Hash, nie der Name — sonst bindet ein bewegter Head einen laufenden Auftrag still um.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Der Auflöser (`resolve_name`, `resolve_reference`) ist als Port deklariert, im Store aber nicht implementiert (Eigentümer: #63, Ziel: Bahn B)

### REQ-KATALOG-03: Ein Anzeigename aus genau 64 hexadezimalen Kleinbuchstaben wird beim Namen verweigert.
Status:     DRAFT
Quelle:     DESK — [ADR 0007](../decisions/0007-catalog-identity.md) Decision 1
Begründung: Otherwise a typed name and a lineage id cannot be told apart. The refusal is at the name, not after a second discriminator.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Gleicher Eigentümer wie REQ-KATALOG-02: die Verweigerung sitzt im noch nicht implementierten Namensweg (Eigentümer: #63, Ziel: Bahn B)

### REQ-KATALOG-04: Name und Beschreibung einer **publizierten** Revision kommen aus ihren authored Bytes, oder sie fehlen ehrlich.
Status:     DRAFT
Quelle:     DESK — [ADR 0007](../decisions/0007-catalog-identity.md) Decision 4
Begründung: Diese Regel gilt der publizierten Auflistung, nicht der Aufnahme — Aufnahme ist REQ-KATALOG-05 und ein anderer Akt. Der Katalog erfindet keine Beschreibung: ein V3-Dokument trägt die Felder, eine V1/V2-Revision nicht, und dann steht dort ehrlich nichts statt einer leeren Zeile.
Journeys:
Beweis:     a-published-revision-is-listed-with-the-name-its-author-wrote
            a-format-that-declares-no-name-is-listed-as-unnamed
            the-description-is-read-from-the-published-bytes-and-from-nowhere-else
Offen:

### REQ-KATALOG-05: Eine nur publizierte Revision ist noch kein wählbares Katalogmitglied.
Status:     DRAFT
Quelle:     DESK — [ADR 0007](../decisions/0007-catalog-identity.md) Decision 3
Begründung: Publication and admission are two acts. The operator must not be offered a name that the catalog has not admitted.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Aufnahme-Befehl und Picker-Mitgliedschaft fehlen; die Tabellen dafür stehen seit #182 (Eigentümer: #63, Ziel: Bahn B)

## Open questions

- **Sharing** is named by the operator as future and is not designed here. (Eigentümer: Operator, Ziel: later requirement issue.)

Wann eine Quelländerung zu einer neuen aufgenommenen Revision wird, steht hier
nicht mehr als offene Frage: [ADR 0007](../decisions/0007-catalog-identity.md)
Decision 2 entscheidet Scan, Intake und Aufnahme und besitzt sie allein.

Der Rest der Schuld, an `bd4883d2` nachgemessen statt aus der Erinnerung
geschrieben: die Lineage-Tabellen `catalog_lineages` und
`catalog_lineage_members` **stehen** (mit #182 gelandet). Offen sind der
Auflöser im Store — `resolve_name` und `resolve_reference` sind als Port
deklariert und haben keine Implementierung —, der Aufnahme-Befehl und der
Picker, der eine Mitgliedschaft und einen Namen anbietet.

## Acceptance

REQ-KATALOG-04 is bound to three sentences declared in
`acceptance/22-where-named-workflows-live.toml`:
`a-published-revision-is-listed-with-the-name-its-author-wrote`,
`a-format-that-declares-no-name-is-listed-as-unnamed` and
`the-description-is-read-from-the-published-bytes-and-from-nowhere-else`. It is
the only bound rule; REQ-KATALOG-01, 02, 03 and 05 are `UNGEBUNDEN`.
