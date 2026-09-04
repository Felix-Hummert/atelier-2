# Requirement 0001: Items are prioritised, get their workflow, and run from a queue

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

### REQ-QUEUE-01: Triage schlägt vor, es entscheidet nicht.
Quelle: DESK — 5302017656 §1 (Regel 1 des Fadens), die Antwort des Desks auf die Operator-Frage „gut oder Push-back?"

### REQ-QUEUE-02: Die Workflow-Zuordnung kommt aus Messwerten.
Quelle: DESK — 5302017656 §2 (Regel 2)

### REQ-QUEUE-03: Der Queue-Deckel wird in dem gezählt, was das Atelier ohne den Provider zählen kann.
Quelle: DESK — 5302131944 §1 (Regel 3), das den undenominierten „Tages-Deckel" von 5302017656 §3 ersetzt

### REQ-QUEUE-04: Rot dreht sich nicht im Kreis.
Quelle: DESK — 5302017656 §4 (Regel 4)

### REQ-QUEUE-05: Die Queue automatisiert das Anstoßen, nie das Freigeben.
Quelle: DESK — 5302017656 §4 (Regel 5), wörtlich gehalten durch 5302109936 und 5302131944; der Faden schreibt den Satz keinem Operator-Satz zu, er steht hier so, wie das Desk ihn schrieb.

### REQ-QUEUE-06: Tiefe Verweise in beide Richtungen, Tracker-neutral.
Quelle: DESK — 5302022156 (Regel 6). Der Kommentar ist mit „ERGÄNZUNG (Operator)" überschrieben, sein Rumpf ist aber Desk-Prosa ohne zitierten Operator-Satz, was die Regel nicht über `DESK` hebt.

### REQ-QUEUE-07: Pause heißt auslaufen, Fortsetzen heißt die Ready-Menge freigeben.
Quelle: DESK — 5302131944 §2 (Regel 7), das die Park-und-Sichern-Formulierung von 5302048197 ersetzt

### REQ-QUEUE-08: Ein Label autorisiert Arbeit nur, wenn der setzende Akteur nicht das Atelier selbst ist.
Quelle: DESK — 5302131944 §3 (Regel 8), das das kanonische Szenario von 5302062963 schärft

### REQ-QUEUE-09: Ein Projekt ist ein konfiguriertes Bündel.
Quelle: DESK — 5302062963 §1 (Regel 9), die Desk-Ableitung aus dem kanonischen Operator-Szenario in der Journey „A labelled item joins the queue and starts"

### REQ-QUEUE-10: Gearbeitet wird in einem Klon je Attempt, nie im Checkout des Operators.
Quelle: DESK — 5302062963 §2 (Regel 10), dieselbe Ableitung

### REQ-QUEUE-11: Ein CI-Tor ist ein Workflow-Detail, kein Teil der Plattform-Anbindung.
Quelle: DESK — 5302062963 §3 (Regel 11), dieselbe Ableitung

### REQ-QUEUE-12: Verarbeitung ist sequenziell oder parallel bis zum Queue-Deckel.
Quelle: DESK — 5302062963 §4 mit 5302048197 (Regel 12)

### REQ-QUEUE-13: Menschliche Tore sind vier genaue Stellen, die der Operator setzt, nie ein globaler Schalter.
Quelle: DESK — 5302732436 (Regel 13). Die vier Stellen sind die Komposition des Desks; der Operator-Satz jenes Kommentars ist der Nordstern unter `Intent` und sagt nur „außer ich schreite irgendwo ein".

### REQ-QUEUE-14: Kein Tracker-Nachbau; Items leben im angebundenen Tracker.
Quelle: OPERATOR — 5307633402 auf #79 (Regel 20), der Operator wörtlich an Fable, 16.08.2026.

### REQ-QUEUE-15: Priorität soll im Atelier sichtbar und bindend sein, nicht im Tracker-Kommentar.
Quelle: DESK — 5307639686 auf #79 (Regel 21), ersetzt Punkt 3 von 5307633402.
