# Requirement 0005: Named lineages, not hashes, are what the operator picks

## Intent

The operator wants to decide which agents and skills he has and where they come
from. Sharing them is a later requirement. (5301973340.)

Named, versioned, proven chains — not a pile of revision hashes — are what new
work is laid back into. (#6; ADR 0007 Context.)

Gewählt wird bei ihrem Namen, was den Namen über ein Neu-Publizieren hinweg
behält — die Lineage. Aufgelöst wird der Name genau einmal, zur Autorenzeit,
bevor die Run-Konfigurations-Revision publiziert wird; danach existieren nur
noch Ids und Hashes. Gebunden ist deshalb der exakte Revisions-Hash, nie der
Name — sonst bindet ein bewegter Head einen laufenden Auftrag still um. Ein
Anzeigename aus 64 hexadezimalen Kleinbuchstaben wäre von einer Lineage-Id
nicht zu unterscheiden; die Verweigerung sitzt deshalb am Namen, nicht hinter
einem zweiten Diskriminator.

Auflistung und Aufnahme sind zwei Akte: REQ-KATALOG-04 gilt der publizierten
Auflistung, die Aufnahme ist REQ-KATALOG-05, und dem Operator wird kein Name
angeboten, den der Katalog nicht aufgenommen hat. Jedes veröffentlichbare
Format ist heute Format 3, und dessen Grammatik verlangt den Namen; die
Beschreibung bleibt optional. Der Katalog erfindet keine Beschreibung: fehlt
sie in den authored Bytes, steht dort ehrlich nichts statt einer leeren Zeile.

## Rules

### REQ-KATALOG-01: Der Operator bestimmt, welche Agenten und Skills er hat und woher sie kommen.
Quelle: OPERATOR — 5301973340, quoting him: „Ich würde gerne bestimmen können, welche Skills und Agenten ich habe und wo sie herkommen"

### REQ-KATALOG-02: Der Operator wählt eine Lineage bei ihrem Namen; durabel gebunden wird danach der exakte Revisions-Hash.
Quelle: DESK — [ADR 0007](../decisions/0007-catalog-identity.md) Decision 1 und Abschnitt 9

### REQ-KATALOG-03: Ein Anzeigename aus genau 64 hexadezimalen Kleinbuchstaben wird beim Namen verweigert.
Quelle: DESK — [ADR 0007](../decisions/0007-catalog-identity.md) Decision 1

### REQ-KATALOG-04: Name und Beschreibung einer **publizierten** Revision kommen aus ihren authored Bytes, oder sie fehlen ehrlich.
Quelle: DESK — [ADR 0007](../decisions/0007-catalog-identity.md) Decision 4

### REQ-KATALOG-05: Eine nur publizierte Revision ist noch kein wählbares Katalogmitglied.
Quelle: DESK — [ADR 0007](../decisions/0007-catalog-identity.md) Decision 3

## Non-goals

Sharing von Agenten, Skills und ganzen Workflows ist vom Operator als spätere
Anforderung benannt und wird hier nicht entworfen (5301973340). Die
Katalog-Identität — Store, Scan, Intake und Aufnahme, und dass Git-Dateien die
Import-Wahrheit sind — besitzt [ADR
0007](../decisions/0007-catalog-identity.md) mit #22; dieses Dokument
wiederholt dessen Entscheidungen nicht. Welche Beweissätze eine Regel binden,
besitzen die Deklarationen unter `acceptance/`.
