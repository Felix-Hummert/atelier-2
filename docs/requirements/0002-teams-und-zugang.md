# Requirement 0002: Access is an invitation, and the installation is the team's workshop

```text
Status:         AGREED
Owner-Issue:    https://github.com/FlexOr2/atelier-2/issues/82
Source-Threads: #82
Distilled-From: 5302604615, 5302806812, 5302820772, 5302849696, 5302855908,
                5307632273
                #82 body, sha256
                fe6fd31f2f8c0aa0866054dc91a3c9ef48843956d508105e414940bff8376868
Approved-By:    5307632273
```

`AGREED` as a reading (operator comment 5307632273, 16.08.2026, „ja passt"): the
operator approved that this document reads its thread faithfully. That
approval settles no direction beyond what the thread itself settles — the
paragraph below keeps saying why that caution matters here. And this subject is the clearest case for why the status exists. The
operator's wish is on the record and quoted verbatim below; almost everything
under it is the desk's answer to his questions, and #82 itself heads that answer
"Engineering-Richtung (zu prüfen im späteren ADR, nicht vorentschieden)".
**Exactly one rule below quotes an operator sentence** — REQ-ZUGANG-15, and only
for the part that sentence carries. REQ-ZUGANG-11 was graded `OPERATOR` in an
earlier revision on the strength of its comment's header "ZWEI PRÄZISIERUNGEN
(Operator)"; the section under that header quotes him nowhere, so it is `DESK`.
The rest is a direction: nothing in it may be built as settled, and its real
binding force is negative — make no decision now that blocks it.

## Intent

The operator's wish, quoted as #82 records it (#82 body @ fe6fd31f):

> „Wir brauchen ein echtes Login und Sicherheit wie im Songmaker oder besser —
> Entra-ID-Login und OAuth ermöglichen, so dass es auch von professionellen
> Firmen verwendet werden kann."

Songmaker is the bar the operator is measuring against. Behind the wish sit two
sentences that decide the shape of the whole subject. Both are the desk's, given
as answers to questions the operator asked, and both are still awaiting his
ruling:

- **Installation is not access. Access is an invitation by the operator — for
  humans and for machines alike.** (5302604615, answering the operator's
  question "jeder der den Client installiert kann dann das Atelier verwenden?".)
- **The installation is the workshop of a team, not of a person.** Today's
  single-person operation is the special case of team size 1. (5302806812 §1,
  answering "personengebunden oder Projekt? für Teams? das Konzept fehlt noch".)

## Rules

Each rule below is the rule this document already carried; the identifier is
what is new, so a later gate can find it and an acceptance sentence can point
back at it. The thread's own numbering is kept in `Quelle`.

### REQ-ZUGANG-01: OIDC ist das eine Protokoll, und das Atelier besitzt niemals Passwörter.
Status:     DRAFT
Quelle:     DESK — #82 body @ fe6fd31f (Regel 1), unter „Engineering-Richtung (zu prüfen im späteren ADR, nicht vorentschieden)"
Begründung: Identitätsanbieter sind Konfiguration — Entra ID, Google, GitHub, Keycloak —, damit fällt „Entra-Unterstützung" aus der Architektur heraus, statt ein Sonderfall zu sein. Passwörter zu speichern wird als Entwurf verweigert, mit derselben Referenz-Disziplin, die für Provider-Token schon gilt.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Die Protokoll-Mechanik ist ausdrücklich einem späteren ADR vorbehalten; bindend ist bis dahin nur die negative Pflicht, nichts zu bauen, das OIDC später verstellt (Eigentümer: #82, Ziel: Zugangs-ADR)

### REQ-ZUGANG-02: Autorisierung sind Rollen je Projekt.
Status:     DRAFT
Quelle:     DESK — #82 body @ fe6fd31f (Regel 2)
Begründung: Viewer / Operator / Admin, gewährt je Projekt. Agenten und Runner bleiben eigene typisierte Akteure — die Akteurs-Typisierung von ADR 0009 §9 ist die bereits gelegte Naht.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Die Projekt-Kopplung gehört [ADR 0011](../decisions/0011-project-isolation.md); der Faden nannte #23, und das Item ist **geschlossen** (Eigentümer: ADR 0011, Ziel: Projekt-Bau)

### REQ-ZUGANG-03: Die Prüfspur beantwortet, wer gestartet, pausiert oder eingegriffen hat.
Status:     DRAFT
Quelle:     DESK — #82 body @ fe6fd31f (Regel 3)
Begründung: Es ist die Attach-Prüfspur von ADR 0009 §8, verallgemeinert. Ohne sie ist ein Eingriff eine Behauptung.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-ZUGANG-04: Lokal bleibt einfach, und die Grenze ist die, die es schon gibt.
Status:     DRAFT
Quelle:     DESK — #82 body @ fe6fd31f (Regel 4), 5302604615
Begründung: Der Einzeloperator-Loopback-Modus braucht keinen Login (ADR 0009 §3); Login wird genau dann Pflicht, wenn ein Nicht-Loopback-Bind geschieht. Die untere Schranke bleibt fail-closed, ohne Zwischenzustand.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Heute bindet `serve --host 0.0.0.0` ohne komponierten Executor öffentlich und unauthentifiziert; die Schranke ist als ADR-Satz da, als Verhalten nicht (Eigentümer: ADR 0009, Ziel: Bind-Kopf)

### REQ-ZUGANG-05: Ein Mensch wird durch Login plus gewährte Rolle zugelassen.
Status:     DRAFT
Quelle:     DESK — 5302604615 (Regel 5)
Begründung: OIDC-Login gegen den konfigurierten IdP *und* eine vom Operator gewährte Rolle je Projekt; ohne beides klopft eine installierte CLI oder Browser-Sitzung nur an. Sitzungen sind kurzlebig, Aktionen geprüft. Bezahlte Starts unterliegen zusätzlich den Budget-Regeln, unabhängig davon, welcher Akteur sie auslöst.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - „Ein installierter Client ohne Einladung erreicht nichts" ist als Kandidat notiert, von keiner Story deklariert (Eigentümer: #82, Ziel: Akzeptanz-Deklaration)

### REQ-ZUGANG-06: Eine Maschine wird durch Einschreibung zugelassen.
Status:     DRAFT
Quelle:     DESK — 5302604615 (Regel 6)
Begründung: Ein einmaliges Join-Token wird gegen das eigene kurzlebige Zertifikat des Runners getauscht und dabei verbraucht (ADR 0009 §4); ein Widerruf trifft danach genau einen Runner.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - „Ein einmaliges Join-Token lässt sich kein zweites Mal verwenden" ist Kandidat, nicht deklariert (Eigentümer: #82, Ziel: Akzeptanz-Deklaration)

### REQ-ZUGANG-07: Die Leitung ist TLS, in beide Richtungen gedacht, aus dem Standard-Stack.
Status:     DRAFT
Quelle:     DESK — 5302604615 (Regel 7)
Begründung: Der Client prüft über das Server-Zertifikat, dass er mit dem echten Atelier spricht; ein Runner nutzt zusätzlich mTLS. Öffentliche Installationen nehmen Let's Encrypt, private eine minimale eigene CA. Keine selbstgebaute Kryptografie.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-ZUGANG-08: Das Projekt ist die Teilungseinheit.
Status:     DRAFT
Quelle:     DESK — 5302806812 §2 (Regel 8)
Begründung: Rollen sind je Projekt — wer sieht, wer startet, wer verwaltet.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-ZUGANG-09: Bibliotheken und Workflows zu teilen heißt, Git-Quellen zu teilen.
Status:     DRAFT
Quelle:     DESK — 5302806812 §3 (Regel 9)
Begründung: Das Modell von ADR 0007 ist der einzige Teilungskanal: Quellen werden global registriert und je Projekt ausgewählt. Es gibt keinen zweiten Kanal.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-ZUGANG-10: Runner sind installationsgebunden und tragen einen Eigentümer-Bereich.
Status:     DRAFT
Quelle:     DESK — 5302806812 §4 (Regel 10)
Begründung: Sie werden am Atelier des Teams über ein Einschreibe-Register angemeldet — das GitLab-Runner-Modell, bewusst konsumiert statt neu erfunden. Ein Runner, der ein *persönliches Abo-Credential* hält, trägt `owner` und `allowed-projects`, damit kein Kollege fremdes Kontingent ausgibt. API-Key-Runner dürfen teamweit sein.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - „Ein Abo-Runner verweigert ein Projekt außerhalb seiner `allowed-projects`" ist Kandidat, nicht deklariert (Eigentümer: #82, Ziel: Akzeptanz-Deklaration)

### REQ-ZUGANG-11: Platzierung folgt der Bezeugbarkeit, nicht der Vorliebe.
Status:     DRAFT
Quelle:     DESK — 5302806812 §5, geschärft durch 5302820772 §1 (Regel 11). Der Kommentar ist „ZWEI PRÄZISIERUNGEN (Operator)" überschrieben, §1 zitiert aber keinen Operator-Satz; erstklassige Remote-Abo-Runner sind damit die Desk-Lesart seiner Position und kein Ruling.
Begründung: Ein Abo braucht eine langlebige Maschine mit einem interaktiven Login — **einschließlich entfernter Maschinen, die erstklassig sind**: eine langlebige Remote-Maschine mit einem einzigen interaktiven Login trägt einen Abo-Executor vollständig, und das Credential-Verzeichnis bleibt lokal, womit das Referenzprinzip unberührt ist. Nur *ephemere* Umgebungen (Wegwerf-CI) können ein Abo strukturell nicht halten und nehmen deshalb API-Keys.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Eine frühere Fassung führte diese Regel als `OPERATOR` auf die Überschrift ihres Kommentars hin; das ist korrigiert und bleibt hier benannt, damit die Korrektur nicht unsichtbar ist (Eigentümer: #82, Ziel: Ruling oder Bestätigung)

### REQ-ZUGANG-12: Verbrauch wird je Modus geführt, und Modi werden nie vermischt.
Status:     DRAFT
Quelle:     DESK — 5302806812 §5 (Regel 12), bestätigt ADR 0008 und #8
Begründung: Beide Modi messen Attempts, Dauer und Token. Geld ist **nur** im Key-Modus exakt; ein Abo ist ein ehrlich beschrifteter Kontingent-Anteil, nie mit Geld vermischt und nie hochgerechnet.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Die Einheiten stehen in ADR 0008; die Modus-Trennung hat keinen Satz (Eigentümer: #8, Ziel: Messwerte-Kopf)

### REQ-ZUGANG-13: Teamweiter API-Key-Verbrauch wird zugerechnet, nicht bloß gemessen.
Status:     DRAFT
Quelle:     DESK — 5302855908 (Regel 13), der eine Befund, der die Maschinen-Durchsicht von 5302849696 überlebt hat
Begründung: Je Projekt und je Auslöser (Akteur oder Workflow), nie nur je Runner. Das macht „welches Projekt oder Teammitglied hat was ausgegeben" strukturell beantwortbar, ohne persönliche Kontingente zu vermischen. Die Zäune `owner` + `allowed-projects` bleiben das Schutzinstrument für *persönliche Abo*-Kontingente; die Asymmetrie ist gerechtfertigt, weil ein API-Key konstruktionsbedingt Team-Abrechnung ist und es dort kein persönliches Kontingent zu zäunen gibt.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-ZUGANG-14: Credentials bleiben Referenzen und werden nie durch das Atelier transportiert.
Status:     DRAFT
Quelle:     DESK — 5302806812 §6 (Regel 14), ADR 0009 §6
Begründung: Zentrale Verteilung wäre die Aufgabe eines Secret-Managers, nicht dieses Produkts.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-ZUGANG-15: Die Geldzeile eines Abos darf eine Schätzung sein, und sie ist provider-neutral.
Status:     DRAFT
Quelle:     OPERATOR — 5302820772 §2 (Regel 15), sein Satz wörtlich:

            > „der Rest könnte eine Schätzung sein — provider-neutral"
Begründung: Das ist der gesamte Operator-Inhalt des Kommentars, und der Grad deckt genau ihn. Der Rest ist Desk-Ausarbeitung und Vorschlag bis zu seinem Ruling: die Schätzung wird als klar beschriftete „≈ geschätzt"-Zeile angezeigt und aus **konfigurierbaren** Preistabellen berechnet, nie hartkodiert; Receipts tragen nur gemessene Werte, und kein Tor rechnet je mit einer Schätzung (ADR 0008 Claim 3 unberührt). Die Trennlinie ist, dass die Anzeigeschicht raten darf und die Beweisschicht nie.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - „Ein Receipt trägt nie einen geschätzten Geldwert" ist Kandidat, nicht deklariert (Eigentümer: #82, Ziel: Akzeptanz-Deklaration)

## Open questions

- **The protocol mechanics are not pre-decided.** The OIDC direction above is
  explicitly to be examined in a later ADR (#82 body @ fe6fd31f). Until then the
  binding obligation on everything built now is negative: session and API auth
  assumptions in frontend and API stay exchangeable, so nothing blocks OIDC
  later.
- **SCIM, provisioning, and multi-tenancy are named, not designed.** (#82 body @
  fe6fd31f.)
- **Sequence.** After the function chain and the start of the remote epic. This
  concept makes #82, #23, and #21 team-consistent; it builds nothing ahead of
  time. (#82 body @ fe6fd31f, 5302806812 closing.)

## Acceptance

No story has declared an acceptance sentence for this requirement yet, so the
list below is a set of candidates and not a set of sentences. The subject is deliberately seam-now,
build-later. What the rules above already state in testable form is, notably:
an installed client without an invitation reaches nothing; a non-loopback bind
without a configured authenticator is refused rather than served; a one-time
join token cannot be used twice; a personal subscription runner refuses a
project outside its `allowed-projects`; a receipt never carries an estimated
money value.

## Provenance note

Rule 13 exists because the concept was read back by the product itself: a
second-opinion workflow revision ran one paid `claude-haiku-4-5` call against a
1,021-character condensation of the team concept, receipt
`1435f55a15ecccdbf297c912a323ea9d55ad5fd9b641788d3ee591bb70ac749e`
(5302849696).

Of that review, exactly one finding survived — the attribution line now standing
as REQ-ZUGANG-13. Its headline charge ("no way to attribute consumption") was false
against the full concept and hit only the condensation, which had dropped the
per-mode consumption tracking to fit the field limit. Its main recommendation —
abolish team-wide API-key runners — contradicted the attestation rule it praised
in the same breath, since ephemeral CI structurally cannot hold a subscription.
The concept therefore stands and gained a clarifying line; it was not corrected.
