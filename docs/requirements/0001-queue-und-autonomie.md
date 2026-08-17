# Requirement 0001: Items are prioritised, get their workflow, and run from a queue

```text
Status:         AGREED
Owner-Issue:    https://github.com/FlexOr2/atelier-2/issues/79
Source-Threads: #79
Distilled-From: 5302017656, 5302022156, 5302048197, 5302062963, 5302109936,
                5302131944, 5302732436, 5307633402, 5307639686
                #79 body, sha256
                9d781a3c15d9f392d5dcd6c466584002029f16713f079074b87726e17698d200
Approved-By:    5307633402+5307639686
```

`AGREED`: the operator approved this reading on 16.08.2026 — 0002/0003/0004
plainly („ja passt"), 0001 conditional on rules 20/21 being incorporated,
which this revision does; the approval objects are named in `Approved-By`.
The thread still wins where they disagree.

## Intent

A project is connected to a tracker — GitHub, GitLab, whichever is configured.
Work items arise there, written by humans or by workflows. From then on it runs
by itself: items are prioritised, get the workflow that fits them, land in a
queue, and start as soon as their preconditions are met — visibly, live. Not
everything is worked automatically; a filter decides what agents may take and
what belongs to a human. (#79 body @ 9d781a3c, which heads this as the
operator's vision and marks it *wörtlich sinngemäß* — a rendering close to his
words, not a transcript; this document repeats that qualifier rather than
promoting the passage to a quotation.)

This is the mechanisation of the way the fleet already works by hand: issue →
triage → queue → precondition-driven start → observable processing. The
conductor in continuous operation instead of once per chat turn. (#79 body @
9d781a3c; the desk's reading of that vision, not part of it.)

The north star the operator stated for this item and its siblings. The thread
records it as *wörtlich sinngemäß* — a rendering close to the operator's words,
not a transcript — and this document repeats it in that form (5302732436):

> „Ich sage meine Vision — und möchte, dass danach mit Workflows alles autonom
> abgearbeitet wird, bis das Produkt fertig ist. Außer ich schreite irgendwo
> ein."

The counter-image is named: today the operator has to nudge the coordinator and
ask whether useful work is still happening. That is the illness the queue cures.

The composition across its owners: vision → vision planning (#6) → breakdown
planner (#80) → item tree with dependencies → the queue (#79) pulls every ready
piece through its workflow until the tree is empty. **Done = every piece of the
epic landed** — the machine-readable definition of "the product is finished".
(5302732436, where it is the desk's composition around the operator's north
star, not a sentence of it.)

## Rules

Each rule below is the same rule this document has carried since it was
distilled; what is new is the identifier, so a later gate can find it and an
acceptance sentence can point back at it. The numbers the thread used are kept
in `Quelle` — the thread jumped from 13 to 20, and that history is worth more
than a tidy sequence.

### REQ-QUEUE-01: Triage schlägt vor, es entscheidet nicht.
Status:     DRAFT
Quelle:     DESK — 5302017656 §1 (Regel 1 des Fadens), die Antwort des Desks auf die Operator-Frage „gut oder Push-back?"
Begründung: Priorität ist Geschäftsurteil. Die Vorgabe ist ein Vorschlag (Priorität + Workflow) mit Ein-Klick-Bestätigung; Auto-Bestätigung wird pro Item-Klasse freigeschaltet, sobald die Scorecard sie verdient hat. Volle Automatik wird verdient, nie angenommen. Der Operator hat das Urteil erbeten und nicht entschieden.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Kein Akzeptanz-Satz spricht die Vorschlags-Zusage aus; er entsteht mit der Queue-Sicht (Eigentümer: #79, Ziel: erster Queue-Kopf)

### REQ-QUEUE-02: Die Workflow-Zuordnung kommt aus Messwerten.
Status:     DRAFT
Quelle:     DESK — 5302017656 §2 (Regel 2)
Begründung: Gespeist aus den #8-Messungen — Workflow × Item-Klasse. Kaltstart mit handgeschriebenen Regeln, danach Lernen. #79 und #8 sind gekoppelt.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Die Messfläche selbst ist #8s (Eigentümer: #8, Ziel: Messwerte vor Zuordnungslernen)

### REQ-QUEUE-03: Der Queue-Deckel wird in dem gezählt, was das Atelier ohne den Provider zählen kann.
Status:     DRAFT
Quelle:     DESK — 5302131944 §1 (Regel 3), das den undenominierten „Tages-Deckel" von 5302017656 §3 ersetzt
Begründung: (a) gestartete Attempts je Zeitfenster und (b) summierte `attempt_deadline_seconds`-Zulassung; optional (c) ein Token-Deckel **je Meter-Revision**, nie eine Summe über Meter-Revisionen hinweg. Der Parallelitäts-Deckel bleibt eine Zählung gleichzeitiger Attempts. Alle drei sind sichtbar und **verweigern, statt zu kürzen**. Ein Tages-Token-Deckel über eine Queue mehrerer Modelle und Provider wäre genau die Quer-Meter-Summe, die ADR 0008 verbietet. Derselbe Zähler ist heute der einzige ehrliche Verbrauchsalarm: er kann dem Operator *vorher* sagen, dass es eng wird. Beide Objekte sind Desk-Kommentare; kein Operator-Ruling hat zwischen ihnen gewählt.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Kein Deckel existiert im Code; die Einheiten stehen in ADR 0008, der Zähler nicht (Eigentümer: #79, Ziel: erster Queue-Kopf)

### REQ-QUEUE-04: Rot dreht sich nicht im Kreis.
Status:     DRAFT
Quelle:     DESK — 5302017656 §4 (Regel 4)
Begründung: Begrenzter Retry, danach Eskalation in den Posteingang. Ein unbegrenzter Retry verbrennt Geld an einem Fehler, den niemand sieht.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Der Posteingang existiert als Oberflächen-Zusage (#9); die Eskalationsregel der Queue hat keinen Beweis (Eigentümer: #79, Ziel: erster Queue-Kopf)

### REQ-QUEUE-05: Die Queue automatisiert das Anstoßen, nie das Freigeben.
Status:     DRAFT
Quelle:     DESK — 5302017656 §4 (Regel 5), wörtlich gehalten durch 5302109936 und 5302131944. Der Faden schreibt den Satz keinem Operator-Satz zu; er steht hier so, wie das Desk ihn schrieb, und ist ein Vorschlag für die Akzeptanz-Formulierung, kein Ruling:

            > „Die Queue automatisiert das ANSTOSSEN, nie das FREIGEBEN — Verdict-Tore in den Workflows bleiben, Durchsatz schlägt nie Review."
Begründung: Das ist der Satz, der dieses Feature von einem Unfall trennt. Durchsatz darf ein Review nie überholen.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Der Satz ist als Akzeptanz-Richtung notiert, aber von keiner Story deklariert (Eigentümer: #79, Ziel: Akzeptanz-Deklaration)

### REQ-QUEUE-06: Tiefe Verweise in beide Richtungen, Tracker-neutral.
Status:     DRAFT
Quelle:     DESK — 5302022156 (Regel 6). Der Kommentar ist mit „ERGÄNZUNG (Operator)" überschrieben, sein Rumpf ist aber Desk-Prosa ohne zitierten Operator-Satz, was die Regel nicht über `DESK` hebt.
Begründung: Jeder Lauf und Workflow in der Atelier-Oberfläche verweist auf sein Work-Item; der Queue-Start schreibt den Rückverweis am Item — auf den lebenden Graphen, zuletzt auf das quittierte Ergebnis. Eigentümer-Schnitt: die Verweis-Semantik (was wann geschrieben wird, idempotent, per Readback geprüft) gehört zum Adapter-Vertrag, die Oberflächen-Seite zu #9.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Der Adapter-Vertrag ist heute [ADR 0010](../decisions/0010-github-platform-adapter.md); der Faden nannte #24, und das Item ist geschlossen (Eigentümer: ADR 0010, Ziel: Adapter-Bau)

### REQ-QUEUE-07: Pause heißt auslaufen, Fortsetzen heißt die Ready-Menge freigeben.
Status:     DRAFT
Quelle:     DESK — 5302131944 §2 (Regel 7), das die Park-und-Sichern-Formulierung von 5302048197 ersetzt
Begründung: Pause startet keinen neuen Knoten und lässt laufende Attempts bis zu ihrem terminalen Receipt auslaufen; Fortsetzen gibt die Ready-Menge wieder frei. „Verlustfrei" meint den Zustand der *Queue* — Item-Reihenfolge, Zuordnungen, erfüllte Vorbedingungen —, nie einen laufenden Attempt: eine native Provider-Sitzung verspricht weder Stream-Replay noch Exactly-once-Modellaufrufe, der Adapter läuft mit `--no-session-persistence`, und ein Abbruch endet in genau einem terminalen Cancel-Receipt. Ein pausierter Attempt wäre ein abgebrochener, dessen Neustart ein neuer bezahlter Aufruf ohne die halbfertige Arbeit ist. **Das verengt eine Zusage, die der Faden dem Operator zuschreibt**: 5302048197 ist „ERGÄNZUNG (Operator)" überschrieben und verspricht „Pause/Park mit sauberer Zustands-Sicherung … jederzeit, verlustfrei", zitiert aber keinen Operator-Satz — unter der Grad-Regel ist auch das Desk-Prosa, und diese Regel korrigiert eine Desk-Lesart mit einer anderen. Sie bleibt markiert, weil die Zusage etwas wiedergeben könnte, das der Operator ungeschrieben gesagt hat.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Die Verengung ist ein Vorschlag an den Operator, keine geschlichtete Korrektur (Eigentümer: Operator, Ziel: Ruling auf #79)

### REQ-QUEUE-08: Ein Label autorisiert Arbeit nur, wenn der setzende Akteur nicht das Atelier selbst ist.
Status:     DRAFT
Quelle:     DESK — 5302131944 §3 (Regel 8), das das kanonische Szenario von 5302062963 schärft
Begründung: Im PAT-Modus sind Labels eine *beobachtete* Menge: die Operations-Registry enthält **keine Schreiboperation** für das autorisierende Label, und die Akteursprüfung läuft über die Abwesenheit des Atelier-Inhaltsmarkers — der Marker ist ein Hinweis, die fehlende Schreiboperation ist die Kontrolle. Im App-Modus ist die Prüfung über die Kontoidentität trivial. Sonst könnte ein Agent seine eigene Arbeit und ihr Geld autorisieren, indem er ein Label schreibt; dafür braucht es keinen Angreifer, ein Fehler oder ein missverstandener Prompt genügt, und Prompts sind keine Kontrolle.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Die Operations-Registry gehört heute [ADR 0010](../decisions/0010-github-platform-adapter.md); der Faden nannte #24, und das Item ist geschlossen (Eigentümer: ADR 0010, Ziel: Adapter-Bau)

### REQ-QUEUE-09: Ein Projekt ist ein konfiguriertes Bündel.
Status:     DRAFT
Quelle:     DESK — 5302062963 §1 (Regel 9), die Desk-Ableitung aus dem kanonischen Operator-Szenario unter `Acceptance`
Begründung: Repository-URL + Tracker + Credential-*Referenz* (nie Klartext, ADR 0009) + Workflow-Zuordnungsregeln + Item-Filter. Eigentümer-Schnitt: die Projekt-Isolation entscheidet [ADR 0011](../decisions/0011-project-isolation.md), den Adapter [ADR 0010](../decisions/0010-github-platform-adapter.md), die Queue-Regeln dieses Dokument.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Der Faden schnitt beide Eigentümer auf Items (#23, #24), die inzwischen **geschlossen** sind; die Sätze zeigen jetzt auf die Records, die sie hervorgebracht haben (Eigentümer: ADR 0010 / ADR 0011, Ziel: deren Umsetzung)

### REQ-QUEUE-10: Gearbeitet wird in einem Klon je Attempt, nie im Checkout des Operators.
Status:     DRAFT
Quelle:     DESK — 5302062963 §2 (Regel 10), dieselbe Ableitung
Begründung: Das Ergebnis kommt als Branch oder Pull Request zurück. Das macht den Werkstatt-Begriff für #60 genau: das attempt-eindeutige Arbeitsverzeichnis wird aus dem Projekt-Klon gesät, während Scratch (#58) daneben die Schreibfläche des Providers bleibt.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Die Werkstatt-Zusage gehört #60/#58 und ist dort noch nicht als Satz deklariert (Eigentümer: #60, Ziel: Werkstatt-Kopf)

### REQ-QUEUE-11: Ein CI-Tor ist ein Workflow-Detail, kein Teil der Plattform-Anbindung.
Status:     DRAFT
Quelle:     DESK — 5302062963 §3 (Regel 11), dieselbe Ableitung
Begründung: Was ein Projekt vor dem Merge verlangt, gehört in seinen Workflow. Die Anbindung an den Tracker trägt es nicht mit.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-QUEUE-12: Verarbeitung ist sequenziell oder parallel bis zum Queue-Deckel.
Status:     DRAFT
Quelle:     DESK — 5302062963 §4 mit 5302048197 (Regel 12)
Begründung: Sichtbar, quittiert, mit Pause ↔ Fortsetzen. Der Deckel ist der aus REQ-QUEUE-03; diese Regel sagt nur, dass beide Betriebsarten unter demselben Deckel stehen.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Hängt an REQ-QUEUE-03 und REQ-QUEUE-07 (Eigentümer: #79, Ziel: erster Queue-Kopf)

### REQ-QUEUE-13: Menschliche Tore sind vier genaue Stellen, die der Operator setzt, nie ein globaler Schalter.
Status:     DRAFT
Quelle:     DESK — 5302732436 (Regel 13). Die vier Stellen sind die Komposition des Desks; der Operator-Satz jenes Kommentars ist der Nordstern unter `Intent` und sagt nur „außer ich schreite irgendwo ein".
Begründung: (1) das **Workflow-Tor** — ein Wait-Knoten: „zeig mir X, bevor du Y tust"; (2) das **Queue-Regel-Tor** — Item-Klassen autonom versus bestätigungspflichtig, der menschliche Filter dieses Items, mit der Stufen-Mechanik von #106: vom Operator gewährt, durch Messung widerrufbar; (3) das **Budget-Tor** — die Deckel von REQ-QUEUE-03: heiß laufen ⇒ sichtbarer Stopp, nie stilles Weiterbrennen; (4) das **Posteingangs-Tor** (#9) — alles, was den Menschen braucht (Frage, Tor, Rot nach begrenzten Retries), kommt als Karte *bei ihm* an, mit Pause ↔ Fortsetzen je Projekt als Not-Aus und Neustart.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - `the-inbox-names-every-run-that-waits-for-a-human` (#131) beweist einen Teil des vierten Tores, nicht die Regel: sie sagt, dass vier Stellen existieren und wer sie setzt (Eigentümer: #79 + #9, Ziel: eigener Satz je Tor)

### REQ-QUEUE-14: Kein Tracker-Nachbau; Items leben im angebundenen Tracker.
Status:     DRAFT
Quelle:     OPERATOR — 5307633402 auf #79 (Regel 20), der Operator wörtlich an Fable, 16.08.:

            > „ich will arbeit priorisieren stimmt, aber wir müssen aufpassen das wir nicht etwas bauen was github oder andere plattformen bieten […] ich will kein work-item tracker nachbauen! sondern wiederverwenden können (github/gitlab oder jira oder was auch immer anbinden)"
Begründung: Der Grad deckt genau diesen Kern. Die Desk-Ausarbeitung um ihn (selbe Quelle, Grad DESK): der Tracker (GitHub zuerst, andere hinter demselben Port) ist die Quelle der Wahrheit für Items — Anlegen, Beschreiben, Kommentieren, Schließen passieren dort; das Atelier besitzt ausschließlich Orchestrierungszustand per Referenz auf die Tracker-ID (Workflow-Zuordnung, Claim/Queue-Zulassung, Run↔Item-Bindung, Beweise/Receipts). Lackmustest je Kopf: „Bietet die Plattform das schon?" → Adapter, nie Nachbau.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Kein Satz prüft heute, dass das Atelier keinen Item-Zustand doppelt hält (Eigentümer: ADR 0010, Ziel: Adapter-Bau)

### REQ-QUEUE-15: Priorität soll im Atelier sichtbar und bindend sein, nicht im Tracker-Kommentar.
Status:     DRAFT
Quelle:     DESK — 5307639686 auf #79 (Regel 21), ersetzt Punkt 3 von 5307633402. Der Operator wörtlich, 16.08.:

            > „das kann schon auch im atelier sichtbar sein? und es muss so sein dass es bindend ist? […] im kommentar sicher nicht"
Begründung: Die zitierte Quelle trägt diesen Satz **nicht positiv**: sie stellt zwei Fragen und weist eine Ablage zurück. Was sie wirklich entscheidet, ist **im Kommentar nicht** — die einzige Festlegung, und sie ist negativ. `OPERATOR` ist deshalb der falsche Grad, solange kein späteres direktes Ruling zitiert wird, und das Zitat bleibt genau deshalb stehen, damit ein Leser die Lücke selbst sieht statt sie glauben zu müssen. Desk-Ausarbeitung (selbe Quelle, Grad DESK): Priorität ist typisierter, dauerhafter, ereignis-historisierter Orchestrierungszustand — Triage-Vorschlag → Operator-Bestätigung (ein Klick in der Atelier-Queue-Sicht) → gebundene Queue-Reihenfolge; der Tracker erhält höchstens eine abgeleitete, nicht-bindende Spiegelung (z. B. Label), klar als Projektion markiert.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Dass Priorität bindender Orchestrierungszustand *ist*, bleibt Desk-Lesart bis zu einem Ruling (Eigentümer: Operator, Ziel: Ruling auf #79)

## Open questions

- **Retention has an owner now; what stays open is when it is built.** Foreign work
  code and foreign issue text land in a store they cannot be removed from again —
  the only way out is discarding the whole SQLite file. The decision belongs to
  [ADR 0011](../decisions/0011-project-isolation.md) decision 3, which owns the
  mechanics and which names this question as pointing at it. That record is
  `PROPOSED` — decision only, nothing implemented — so what a store does today is
  what the code does, not what the decision says. The build is due *before* a
  second, especially a foreign, project is connected. (5302109936 §4b, assigned in
  5302131944 §4b; ownership passed from #23, closed 2026-08-15, to the record that
  issue produced.)
- **Reachability is a named precondition, not a given.** ADR 0009 refuses every
  non-loopback bind without an operator authenticator, and today's situation is
  sharper than that draft: `serve --host 0.0.0.0` without a composed Claude
  executor binds publicly and entirely unauthenticated. (5302109936 §4a,
  5302131944 §4a.)
- **Whether a personal subscription may be spent on someone else's work code is
  not a technical question** and no technical item answers it. (5302109936 §4c.)
- **The stated order is much shorter than the real chain.** This item sits
  behind #15 → #58 → #60 → #38 → the V3 chain → #16 phase 2 + #63 → the #24
  implementation → #23: eight owners. The vision is not wrong; the sentence
  "Reihenfolge: nach Funktions-Kette (#60/#38) und #24" is just far shorter than
  the chain it names. (5302109936 §5.)

  Nachgeführt am heutigen Stand, weil die Quelle von 2026-08 zwei Glieder als
  *nicht planbar* führte: **#23 und #63 sind geschlossen**, und die V3-Grundlage
  ist gelandet. Der Satz „zwei davon nicht planbar" galt für den Stand seiner
  Quelle und gilt nicht mehr; die Kette ist kürzer, nicht anders. Offen in ihr
  bleibt #16 Phase 2.

## Acceptance

The canonical acceptance scenario is the operator's own, and the thread states
that this sentence *is* the acceptance test of this item. It is recorded there
as *wörtlich sinngemäß* and repeated here in that form (5302062963):

> „Ich binde extern GitHub (oder GitLab — Beispiel) mit einem Token an,
> referenziere das Projekt. Ich setze den Filter: *bearbeite alle Issues, die
> `backlog` als Label im Projekt haben, und nutze den XXX-Workflow.* Dann Start
> — und es arbeitet alles nacheinander weg. Ich kann das Atelier so auch von
> meiner Arbeit aus nutzen, zum Test."

The acceptance direction, which #79 itself heads "zu verfeinern vor Bau" and
which is therefore a direction and not a set of sentences (#79 body @
9d781a3c), plus the desk sentence REQ-QUEUE-05 keeps word for word:

- A newly created item appears in the queue already triaged: priority, assigned
  workflow, and ready state with its open preconditions named.
- A filter or rule set decides which items may run automatically; everything
  else waits visibly for a human.
- Becoming ready starts the bound workflow with no further handling, and the run
  is live on the graph (#9).
- Everything is receipted; no start outside the budget and authorisation rules.
- Word for word, from REQ-QUEUE-05 and authored by the desk: „Die Queue automatisiert
  das ANSTOSSEN, nie das FREIGEBEN — Verdict-Tore in den Workflows bleiben,
  Durchsatz schlägt nie Review."
- The canonical scenario above, carrying the two named preconditions from
  `Open questions` (reachability, retention).

No story has declared any of these as an acceptance sentence yet, so none of
them carries a sentence identifier or a proof.
