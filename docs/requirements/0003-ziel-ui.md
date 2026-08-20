# Requirement 0003: One workshop, three views, one language — the graph

```text
Status:         AGREED
Owner-Issue:    https://github.com/FlexOr2/atelier-2/issues/9
Source-Threads: #9, #5, #336
Distilled-From: 5294009202, 5294316639, 5301898411, 5302066517, 5302109868,
                5302132001, 5302769095, 5302788411, 5307632332,
                5324914874, 5333831486
                #9 body, sha256
                36800d6ecd5d3e8922028425835b368b42d163098e5d32da930e40d25f49ce99
                #336 body, sha256
                92d5e087748fb22ce6b01fd3a5918bd386e6dd8a80f1699105b67ce44198f9a8
Approved-By:    5307632332
```

`AGREED` as a reading (operator comment 5307632332, 16.08.2026, „ja passt"): the
operator approved that this document reads its thread faithfully. That
approval settles no direction beyond what the thread itself settles — the
paragraph below keeps saying why that caution matters here. #9 is a vision, and it says so itself — its body closes "Status:
VISION/PROPOSAL — verbindlich erst als eigenes Requirement-Issue nach
Operator-Abnahme (Konvention siehe #5)". That approval has not happened, so this
document settles nothing, and the route it names is #5's: the operator's approval
publishes a requirement issue, never this file.

The quality section below is an addendum from #336 after that approval. Those
sentences are `DRAFT`. Their identifiers are `REQ-UIQ-nn` because the
acceptance gate publishes `REQ-[A-Z0-9]+-[0-9]{2}` and refuses the hyphen the
item wrote as `REQ-UI-Q`.

The thread quotes the operator in several places, and two of those
quotations state a rule. **Exactly two rules below are graded `OPERATOR`** —
REQ-UI-01, on the sentence 5302769095 quotes from him, and REQ-UIQ-09, on the
quality sentence #336 records as his. The other quotations are his
standard, reproduced under `Intent`; his nine words of dissatisfaction with the
mockup's Settings screen, reproduced at REQ-UI-15; and the questions he asked,
whose answers are the desk's. An earlier revision graded eleven rules
`OPERATOR` on the strength of
comment headers — "OPERATOR-VISION VERSCHÄRFT", "Operator-Feedback-Runde 2",
"OPERATOR-ENTSCHEIDUNG (bindend)" — which the convention now names for what they
are: the desk attributing its own prose. Everything not graded `OPERATOR` is the
desk's reading of the same thread and binds nothing until he rules it.

## Intent

The operator sees every run as a living graph — event-sourced, streamed — with
attempts optionally as **ephemeral tiles** that spawn and end with the attempt,
never as permanent seats. Intervening uses the *native* interactivity of the
provider console; none of it is rebuilt. (#9 body @ 36800d6e, 5294009202.)

Around that core sits the target UI: one workshop, three views, one language —
the graph. (5301898411.)

The operator's standard for it, as 5301898411 records it („Operator-Maßstab,
wörtlich"):

> „simple — ich kann alles machen was ich will — alles einstellen wenn ich will.
> Intuitiv und es muss Spaß machen und cool aussehen. Und natürlich alles
> funktionieren."

And the admission criterion for anything added to it: it must **remove work**,
not add features. (5302788411.)

#336 records a second operator standard, the quality contract (19.08.,
wörtlich destilliert in the item body @ 92d5e087):

> „klar strukturiert, intuitiv, einfach zu bedienen, kein unnützer Schnickschnack,
> ich muss schnell finden was ich brauche — es darf trotzdem geil aussehen und
> Spaß machen."

The nine sentences under Quality are the desk's named criteria and
verifications for that standard. Mockup v4 remains the Gestalt owner.

## Rules

Jede Regel unten ist die Regel, die dieses Dokument seit seiner Destillation
trägt; neu ist der Bezeichner, damit ein Gate sie findet und ein Akzeptanz-Satz
auf sie zurückzeigen kann. Die Nummerierung des Fadens steht in `Quelle`.

### REQ-UI-01: Das Projekt ist die primäre Struktur, kein Topbar-Schalter.
Status:     DRAFT
Quelle:     OPERATOR — 5302769095 (Regel 1), das den Operator beim Mockup v3 zitiert:

            > „das Studio hat den Chat, es sollte die aktiven Projekte zeigen; das Projekt hat die laufenden Workflows"
Begründung: Drei Ebenen. Was der Grad deckt, ist genau dieser Satz; die Karten- und Posteingangs-Details darunter sind Desk-Detail. (1) **Studio (Zuhause)** — Chat mit dem Dirigenten, global und kontextbewusst, dazu die aktiven Projekte als *lebende Karten* („`<projekt>` · n laufend · n wartet auf dich · letzte Landung vor X"), dazu eine Posteingangs-Zeile quer über den Kopf, projektübergreifend, die trägt, was den Menschen braucht. (2) **Projekt** — die Queue (bereit / laufend / wartend), die laufenden Workflows als *Mini-Graphen* (der „das wird gerade getan"-Blick), Regeln und Quellen, Pause ↔ Fortsetzen. (3) **Run** — die volle Leinwand mit Knoten, Kacheln und Eingriff. Der Chat ist auf jeder Ebene dieselbe Tür und trägt den Kontext dieser Ebene: Studio = alles, Projekt = dieses, Run = dieser.
Journeys:
Beweis:     the-workshop-opens-in-the-studio
            every-level-names-the-way-back-up
            the-deepest-level-shows-the-whole-way-it-sits-on
            a-level-opens-from-a-pasted-link-and-survives-a-reload
            the-inbox-names-every-run-that-waits-for-a-human
Offen:      - Die Ebene **Projekt** ist als Adresse und Weg bewiesen, ihr Inhalt (Queue, Mini-Graphen, Regeln, Pause ↔ Fortsetzen) nicht (Eigentümer: #131-Familie, Ziel: Projekt-Ebene)

### REQ-UI-02: Jeder Bildschirm beantwortet genau eine Frage.
Status:     DRAFT
Quelle:     DESK — 5302788411 (Regel 2), die Kuratierung des Desks als Antwort auf die Operator-Frage „was noch einbauen, ohne zu überladen?"
Begründung: Studio: was passiert? Projekt: was passiert hier? Run: was tut er? Bibliothek: was habe ich? Einstellungen: was gilt? Zwei Fragen ⇒ der Bildschirm wird geteilt.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-UI-03: Zwei Türen führen auf dieselbe Leinwand.
Status:     DRAFT
Quelle:     DESK — 5301898411 §2 (Regel 3)
Begründung: Entweder komponiert der Chat mit dem Dirigenten (#7) einen Graphen und zeigt ihn *vor* dem Start, oder der Baukasten lässt Agenten, Skills und Subworkflows aus der Bibliothek auf die Leinwand ziehen, verbinden und per Klick konfigurieren — Modell, Werkzeuge, Budget, Prompt: alles setzbar, nichts, was gesetzt werden muss. Beide Türen erzeugen dasselbe Objekt, den V3-Graphen. Der Dirigent füllt vor, der Mensch justiert.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - 5301898411 ist „OPERATOR-VISION VERSCHÄRFT (Felix …)" überschrieben; der einzige daraus zitierte Operator-Satz ist der Standard unter `Intent`, §1 bis §5 sind Desk-Prosa. Diese Regel bleibt deshalb `DESK`, so getreu sie die Vision auch wiedergibt (Eigentümer: Operator, Ziel: Ruling)

### REQ-UI-04: Die Projektwahl kommt zuerst.
Status:     DRAFT
Quelle:     DESK — 5301898411 §1 (Regel 4)
Begründung: Woran die Flotte arbeitet, ist erstklassig. Dieselbe Grad-Feststellung wie bei REQ-UI-03 gilt hier: der Kommentarkopf nennt den Operator, der Rumpf zitiert ihn nicht.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-UI-05: Die Bibliothek zeigt Namen, nie Hashes.
Status:     DRAFT
Quelle:     DESK — 5301898411 §3 (Regel 5)
Begründung: Benannte, beschriebene, versionierte Agenten im Markdown-Format von #66, Skills und Workflows mit ihrer Scorecard (#8). Ein Hash ist nie wieder eine Auswahlmöglichkeit. Dieselbe Grad-Feststellung wie bei REQ-UI-03.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Die Namen besitzt heute [ADR 0007](../decisions/0007-catalog-identity.md); der Faden nannte #22, und das Item ist **geschlossen** (Eigentümer: ADR 0007, Ziel: Katalog-Oberfläche)

### REQ-UI-06: Derselbe Graph ist die Live-Sicht.
Status:     DRAFT
Quelle:     DESK — 5301898411 §4, 5302066517 §2 (Regel 6)
Begründung: Knoten leuchten arbeitend / abgeschlossen / gescheitert; ein Klick auf einen laufenden Agenten öffnet seine ephemere Kachel mit Live-Ausgabe und nativem Eingriff. Ein laufender Run in der Run-Liste öffnet denselben Graphen in seinem Live-Zustand. Zur Einordnung von REQ-UI-03 bis REQ-UI-06: sie lesen die nummerierten Abschnitte von 5301898411, einem Kommentar mit dem Kopf „OPERATOR-VISION VERSCHÄRFT (Felix …)". Der eine Satz, den er von ihm zitiert, ist der Standard unter `Intent`; §1 bis §5 sind Desk-Prosa, weshalb diese vier Regeln `DESK` sind, so getreu sie die Vision auch wiedergeben.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-UI-07: Der Modus ist eine Fähigkeits-Erklärung.
Status:     DRAFT
Quelle:     DESK — #9 body @ 36800d6e, das diesen Absatz „OPERATOR-ENTSCHEIDUNG (bindend)" markiert, und Kommentar 5294316639 auf #5, der dieselbe Entscheidung in denselben Worten festhält (Regel 7)
Begründung: Headless ist für jeden Provider Pflicht; interaktiv wird erklärt. Ein Knoten, der interaktiv verlangt, scheitert bei der *Validierung* an einem Provider, der es nicht erklärt — nie stillschweigend. Das Enum-Feld gehört in die B0.1-Bindung, bevor der Executor-Vertrag einfriert. Der Grad ist `DESK`, weil beide Objekte Desk-Prosa sind, die die Entscheidung *als* die des Operators etikettiert; keines zitiert ihn. Das ist eine Aussage darüber, was dieser Faden belegt, keine Erlaubnis, die Entscheidung wieder zu öffnen: sie steht, wo sie getroffen wurde, die B0.1-Bindung folgt ihr, und wer sie geschlichtet braucht, fragt den Operator, statt aus dieser Datei eine Beglaubigung zu lesen.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-UI-08: Interaktives Attach ist in V1 nur lokal, und es markiert den Lauf.
Status:     DRAFT
Quelle:     DESK — #9 body @ 36800d6e Teil 2, 5302132001 §2 Schluss (Regel 8) — beides Desk-Text; der Rumpf etikettiert nur REQ-UI-07 als Operator-Entscheidung
Begründung: Der Operator wählt den Modus je Knoten oder Lauf. Interaktive Attempts laufen in derselben isolierten Werkstatt mit derselben erklärten Fähigkeitsmenge; wo diese Gleichheit nicht erzwungen werden kann, ist interaktiv für den Knoten UNVERFÜGBAR — fail-closed. Ein interaktiver Knoten erklärt entweder keine typisierte Ausgabe (nur ohne nachgelagerte Zuordnung zulässig) oder endet mit einem ausdrücklich vom Operator bestätigten Artefakt. Der Lauf und seine nachgelagerten Ausgaben gelten als *operator-beeinflusst* und sind aus der #8-Bilanz ausgeschlossen. Die Grenze ist eine zustandsändernde Handlung zwischen Start und Terminal: **reine Beobachtung berührt nie** — eine Hover-Vorschau oder eine geöffnete Kachel ohne Eingabe markiert den Lauf nicht.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-UI-09: Die Live-Kachel liest den ephemeren Runner-Kanal; rohe Provider-Frames erreichen nie ein Ereignis oder ein Receipt.
Status:     DRAFT
Quelle:     DESK — 5302132001 §2 (Regel 9), das 5302066517 §3 und Teil 2 des Item-Rumpfes schärft
Begründung: Die Invariante von Issue #1 ist absolut: rohe Provider-Frames erscheinen nie in Ereignis, Log, Receipt, Datenbank, Crash-Evidenz oder Werkstatt. **Live-Sicht = ephemerer Runner-Kanal:** Kachel und Hover-Vorschau lesen den ephemeren Kanal des Runners — denselben Weg, den ADR 0009 für Attach entscheidet, mit Ticket je Attach und Attach-Prüfspur. Was dort fließt, ist eine redigierte, nicht-durable Projektion: kein Ereignis, kein Receipt, nirgends gespeichert. Ein Zuschauer sieht die Arbeit; die Datenbank nie. **Durabel ist Hash plus begrenzte Ausgabe:** das Receipt trägt Hash und Ort, nie die Transkript-Bytes. Eine Archivkopie — falls es je eine gibt — liegt außerhalb des Receipts und unter der Aufbewahrungs-Entscheidung, die [ADR 0011](../decisions/0011-project-isolation.md) Entscheidung 3 besitzt und die `PROPOSED` ist, nicht gebaut. Bis sie gebaut ist, gibt es keine Archivkopie. Die Folge für die Roadmap ist ausdrücklich: eine Live-Sicht ist Architektur — ein Streaming-Modus, ein Runner-Kanal, Redaktion —, keine UI-Zeile.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-UI-10: Abgeschlossene Läufe bekommen Historien-Wiedergabe, keine byte-genaue.
Status:     DRAFT
Quelle:     DESK — 5302132001 §1 (Regel 10), das die Formulierung „byte-genau aus den Receipts nachspielbar" von 5302066517 §2 ersetzt
Begründung: Die Schaltfläche spielt die durable Beweiskette nach: Knotenübergänge, gebundene Revisionen und Hashes, erklärte Ausgaben, terminale Receipts — und sie zählt neben sich auf, was wiedergebbar ist. Was nicht wiedergebbar ist, behauptet die Oberfläche nicht. Heute nicht durabel: Zwischenschritte, Werkzeugaufrufe, stderr und überhaupt jede Zeitachse. **Das verengt eine Zusage aus einem Kommentar, der als der des Operators überschrieben ist:** 5302066517 trägt den Kopf „Operator-Feedback-Runde 2" und verspricht in §2 byte-genaue Wiedergabe, zitiert aber keinen Operator-Satz — die Zusage ist damit Desk-Prosa, und diese Regel korrigiert eine Desk-Lesart mit einer anderen. Sie bleibt trotzdem markiert, weil die Zusage etwas wiedergeben könnte, das er am Mockup gesagt hat: die Lesart des Desks ist, dass nichts Durables heute sie erzeugen könnte, und diese Lesart ist ein Vorschlag an ihn, keine geschlichtete Korrektur.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-UI-11: V1 baut Projekt-Gedächtnis; Gedächtnis je Benutzer hängt an #82.
Status:     DRAFT
Quelle:     DESK — 5302132001 §3 (Regel 11), das „Per-User/Projekt-Gedächtnis (später)" von 5301898411 §5 ersetzt
Begründung: Eigene Agenten, Favoriten und Vorgaben hängen am Projekt, das ohnehin erstklassig ist, und das ist ohne einen Begriff von Identität definierbar. Gedächtnis je Benutzer ist ein benannter Nachfolger mit ausdrücklicher Abhängigkeit von Requirement 0002 (#82) — davor gibt es keinen Benutzer, an dem etwas hängen könnte. Dasselbe gilt für den Einstellungsbereich „Users / Audit": er ist #82-gebunden, nicht „später".
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Die Projekt-Erstklassigkeit besitzt heute [ADR 0011](../decisions/0011-project-isolation.md) zusammen mit #79; der Faden nannte #23, und das Item ist **geschlossen** (Eigentümer: ADR 0011, Ziel: Projekt-Bau)

### REQ-UI-12: Sprache und Benennung.
Status:     DRAFT
Quelle:     DESK — 5302066517 §1 (Regel 12)
Begründung: Englisch als Vorgabe, Deutsch optional; kurze Namen — „Studio", nicht „Leinwand & Chat".
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-UI-13: Die Run-Liste zeigt Zweck und Ergebnis, nie nur Status.
Status:     DRAFT
Quelle:     DESK — 5302066517 §4 (Regel 13)
Begründung: Ein Projekt-Chip in jeder Zeile plus ein Projektfilter, und Verbrauch oder Kosten je Zeile (#8).
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-UI-14: Ein austauschbares Design-Token-System.
Status:     DRAFT
Quelle:     DESK — 5302066517 §5 (Regel 14); die ⌘K-Befehlspalette aus demselben Kommentar ist nach 5302132001 zurückgestellt, bis es genug zu befehlen gibt
Begründung: Hell / dunkel / automatisch überall, und Benachrichtigungen als Posteingang — Karten, wenn ein Lauf einen Menschen braucht.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-UI-15: Einstellungen sind eine professionelle Fläche ohne hartkodierte Provider-Zeilen.
Status:     DRAFT
Quelle:     DESK — 5302066517 §6 (Regel 15). Der Operator-Inhalt hinter dieser Regel ist ein Satz:

            > „wie sie aufgebaut ist mag ich noch nicht"
Begründung: Neun Worte über den Einstellungs-Bildschirm des Mockups, und sie sagen nur, was er nicht will. Alles, was die Regel an ihre Stelle setzt, ist die Antwort des Desks und bindet nichts, bis er es entscheidet: „Providers" als Liste verbundener Provider („+ Add provider" → Provider → Login *oder* Token, die Methode ist die Wahl des Operators); dann Projekte (Repository + Tracker + Credential-Referenz + Regeln, Pause ↔ Fortsetzen), Agenten-Vorgaben (Modell je Rollenklasse — Tore und Urteile Opus, Stichproben Haiku; Budget-Rahmen, die nie im Weg und nie aus sind), Runner, Benachrichtigungen, Erscheinung / Sprache. Zur Einordnung von REQ-UI-12 bis REQ-UI-15: alle vier stammen aus 5302066517, dessen Kopf eine Operator-Feedback-Runde nennt; §1, §4 und §5 zitieren ihn nirgends, und §6 zitiert nur die Ablehnung oben.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-UI-16: Vier Zutaten, die Arbeit wegnehmen.
Status:     DRAFT
Quelle:     DESK — 5302788411 (Regel 16), die Kuratierung des Desks; auch das Zulassungskriterium „nimmt Arbeit weg" ist die des Desks
Begründung: (1) **Werkstatt-Puls** — eine immer sichtbare Kopfzeile: heute verbrauchtes Kontingent, Runner-Gesundheit, Queue-Tiefe. Kein Dashboard; sie schafft die Atelier-1-Überraschung ab, per Konstruktion in die 100-%-Grenze zu laufen. (2) **Das Receipt als Schmuckstück** — jedes ✓ öffnet eine schöne, menschenlesbare Receipt-Seite (was lief, was es sah, was es prüfte, die Hash-Kette als Grafik). Das Unterscheidungsmerkmal verdient Juwelier-Behandlung; es ist die spätere Heimat des Dossier-Exports (#104). (3) **Lehrende Leerzustände** — jeder leere Bildschirm zeigt *die eine* nächste Handlung. Onboarding ohne Tutorial. (4) **Rückgängig statt Nachfragen** — ein Fünf-Sekunden-Rückgängig-Hinweis statt Bestätigungsdialogen. Bewusst ausgeschlossen: Dashboards neben dem Puls, ein Benachrichtigungszentrum neben dem Posteingang, Diagramme auf der Startseite.
Journeys:
Beweis:     an-empty-area-names-the-one-next-action
Offen:      - Bewiesen ist die dritte Zutat, die lehrenden Leerzustände. Puls, Receipt-Seite und Rückgängig-Hinweis haben keinen Satz (Eigentümer: #131-Familie, Ziel: eigene Sätze je Zutat)

### REQ-UI-17: Authentifizierung erhöht die Komplexität keines Bildschirms.
Status:     DRAFT
Quelle:     DESK — 5302788411 (Regel 17)
Begründung: Login ist die Seite davor; Identität ist das Avatar-Menü; Rollen ändern, was *gerendert* wird — ein Viewer sieht keine Start-Schaltfläche, statt Verweigerungen zu sammeln. Wegen dieser Regel darf Authentifizierung (Requirement 0002, #82) später kommen, ohne den Entwurf zu verbiegen.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-UI-18: Mockups sind Entwurfs-Vorlagen.
Status:     DRAFT
Quelle:     DESK — 5302769095, das dies als „Mockups sind Design-VORLAGEN (Operator ausdrücklich)" schreibt — eine Klammer-Zuschreibung, kein zitierter Satz, weshalb die Regel `DESK` ist, obwohl derselbe Kommentar ihn für REQ-UI-01 zitiert; 5302066517 Schluss (Regel 18)
Begründung: Die gebaute Oberfläche darf besser oder anders aussehen; bindend sind die Informationshierarchie von REQ-UI-01 und dass jede Anzeige quittierte Wahrheit zeigt. Der aktuelle Stand der Vorlage ist Mockup v4 und liegt als [0003-ziel-ui-mockup-v4.html](0003-ziel-ui-mockup-v4.html) in diesem Repository — im Browser zu öffnen, klickbar, damit jeder Kopf sieht, wie die Ziel-UI aussehen soll (Operator-Auftrag 18.08.2026). Das lebende Original bleibt beim Operator unter derselben Artefakt-URL wie v1 bis v3; ändert er es dort, wird die Datei hier regeneriert, nie unabhängig editiert.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-UI-19: Atelier 1 wird als Konzepte und Lehren wiederverwendet, nie als portierter Code.
Status:     DRAFT
Quelle:     DESK — #9 body @ 36800d6e (Regel 19)
Begründung: Die konkreten Lehren werden als Vorwissens-Notiz gehalten — ttyd-Version und SHA-Pinning, Codex-Vertrauen je Verzeichnis, tmux ≥ 3.4 in Containern wegen der argv0-Ausgabe, Capture-Pane-Diagnose —, damit „Wiederverwendung" nie die Dauersitz-Maschinerie zurückschmuggelt.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### Quality

Nine criteria, each with the verification #336 named. The first two machine
checkers — axe-core over the core surfaces, and a pseudo-locale smoke over
the display strings that already have an owner — run in CI. The rest stay
named rituals or later checkers.

### REQ-UIQ-01: Jedes Element beantwortet eine benennbare Nutzerfrage.
Status:     DRAFT
Quelle:     DESK — #336 body @ 92d5e087, Kriterium 1
Begründung: Keine Antwort → das Element fällt. Die Verifikation ist ein Review-Ritual je Fläche, kein Maschinen-Gate.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Review-Ritual je Fläche ist nicht gebaut (Eigentümer: #336, Ziel: späterer Prüfer)

### REQ-UIQ-02: Kernaufgaben erreichen ihr Ziel in einem benannten Klick- und Blick-Budget.
Status:     DRAFT
Quelle:     DESK — #336 body @ 92d5e087, Kriterium 2
Begründung: Findability ist ein Budget, kein Gefühl. Die Verifikation ist ein Aufgaben-Walkthrough mit Budget.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Klick- und Blick-Budgets sind nicht gebaut (Eigentümer: #336, Ziel: späterer Prüfer)

### REQ-UIQ-03: Begriffe einer Fläche kommen aus einer Quelle.
Status:     DRAFT
Quelle:     DESK — #336 body @ 92d5e087, Kriterium 3
Begründung: Prompt, Output und Log haben ihr Ruling an #333. Diese Regel ist der Owner-Satz; der Pseudo-Locale-Smoke unter REQ-UIQ-04 prüft nur die Display-Strings, die heute schon einen Owner haben.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Log-Fläche (Eigentümer: #104). Prompt und Output spricht die Run-Seite seit #333; das Review-Ritual dieser Regel bleibt ungebunden.

### REQ-UIQ-04: Anzeige-Strings einer Fläche kommen aus ihrem Owner, und das Layout verträgt die längere Form.
Status:     DRAFT
Quelle:     DESK — #336 body @ 92d5e087, Kriterium 4
Begründung: Der erste Maschinen-Prüfer ist ein Pseudo-Locale über die Kernflächen. Heutiger Owner: die Schienen-Wörter in `WORKSHOP_DESTINATIONS`. Alles andere auf Studio, New Run und der Run-Seite ist die benannte Lücke.
Journeys:
Beweis:     core-surfaces-render-owned-display-strings-under-a-pseudo-locale
Offen:      - Die übrigen hartkodierten Anzeige-Strings der Kernflächen (Eigentümer: #336, Ziel: eigener Owner je Fläche)

### REQ-UIQ-05: Die Kernflächen erfüllen WCAG 2.2 AA, oder der Verstoß trägt ein Item.
Status:     DRAFT
Quelle:     DESK — #336 body @ 92d5e087, Kriterium 5
Begründung: Semantik, Kontrast, Screenreader, vollständiger Tastaturweg mit sichtbarem Fokus. Der erste Maschinen-Prüfer ist axe-core im Chromium-E2E über Studio, New Run und die Run-Seite. Die erste Messung (19.08.2026, 66 WCAG-2.2-AA-Regeln) fand keine Verstöße; die Baseline ist deshalb leer. Ein späterer Verstoß ohne Zeile mit Issue-URL ist rot. Der Tastatur-Walkthrough bleibt der zweite, noch ungebundene Prüfer.
Journeys:
Beweis:     core-surfaces-have-no-unnamed-axe-violations
Offen:      - Tastatur-Walkthrough (Eigentümer: #336, Ziel: späterer Prüfer)

### REQ-UIQ-06: Leer, lädt, Fehler und wartet sind vier benannte Zustände.
Status:     DRAFT
Quelle:     DESK — #336 body @ 92d5e087, Kriterium 6
Begründung: Die Studio-, Project- und New-Run-Workflow-Belege tragen Leer, lädt und Fehler für ihre vollständigen Reads. Diese Regel verlangt weiterhin, dass die vier Zustände nicht ineinanderfallen.
Journeys:
Beweis:     the-studio-preserves-confirmed-truth-and-retries-only-its-failed-read the-project-preserves-confirmed-truth-and-retries-only-its-failed-read new-run-preserves-workflow-truth-and-retries-only-the-workflow-read
Offen:      - Der New-Run-Agent-Read sowie der eigene Zustand „wartet“ (Eigentümer: #440 für die Read-Flächen; „wartet“ derzeit ungebunden)

### REQ-UIQ-07: Eine Frage hat ein Muster, und das Muster ist eine wiederverwendete Komponente.
Status:     DRAFT
Quelle:     DESK — #336 body @ 92d5e087, Kriterium 7
Begründung: Konsistenz ist Review plus Komponenten-Inventar, kein axe-Lauf.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Komponenten-Inventar (Eigentümer: #336, Ziel: späterer Prüfer)

### REQ-UIQ-08: Eine Fläche, die ihr Interaktions-Budget überschreitet, ist ein Defekt.
Status:     DRAFT
Quelle:     DESK — #336 body @ 92d5e087, Kriterium 8
Begründung: Geschwindigkeit wird gemessen, nicht geschätzt. Dieser Kopf baut die Messung nicht.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Interaktions-Budget und E2E-Messung (Eigentümer: #336, Ziel: späterer Prüfer)

### REQ-UIQ-09: Die Fläche darf geil aussehen und Spaß machen; das letzte Wort hat der Operator.
Status:     DRAFT
Quelle:     OPERATOR — #336 body @ 92d5e087, wörtlich:

            > „es darf trotzdem geil aussehen und Spaß machen"

            Die Verifikation — Screenshot-Review gegen Mockup v4 — ist Desk-Detail.
Begründung: Bewusst subjektiv. Der Operator-Maßstab unter Intent („simple, intuitiv, Spaß, cool") bleibt das Kriterium; kein Maschinen-Gate spricht das letzte Wort.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Screenshot-Review-Ritual gegen Mockup v4 (Eigentümer: Operator, Ziel: späterer Prüfer)

## Open questions

- **Remote attach is its own epic**, gated on a runner-trust ADR: runner
  identity and registration, operator auth, per-attach step-up, short-lived
  terminal tokens, attach audit. Location transparency stays the *goal*, as a
  designed seam — nothing bakes in localhost — but not as a V1 build. ADR 0001's
  supervision contract is explicitly local-runner scope; remote runners need
  their own attempt-ownership contract (lease, heartbeat, fencing) that
  preserves exactly-once and cleanup. (#9 body @ 36800d6e part 3.)
- **The canvas is far away, and the requirement should say so.** "Both doors
  produce the same object" is the composed-preview invariant of V3-6 (#83),
  which sits behind the chain #41 → #46 → #49 → #54 → #69 — 16 of 22 units of
  critical path, at most two usable builders, and all five still drafts. "The
  conductor pre-fills" additionally depends on #7, which sits behind #38, actor
  identity, and the catalogue. Named so the next prioritisation does not read
  "soon". (5302109868 §4, 5302132001 closing.)
- **`MAXIMUM_WORKFLOW_NODES = 100` is a hard ceiling** a drawing board will make
  visible quickly. (5302109868 §4.)
- **A graph composed on the canvas has no home under ADR 0007** ("atelier-2
  never writes a source"). **Ungelöst**, und hier nicht als kommende Ergänzung
  eines angenommenen Records geführt: die gelandete ADR 0007 enthält den Token
  `unsourced` nicht, und ein Requirement darf einem akzeptierten Record keine
  Regel zuschreiben, die er nicht trägt. Die Desk-Lesart aus der Quelle — eine
  Art `unsourced`, mit Export als Weg *zu* einer Quelle und nie als Schreiben
  *in* sie — bleibt zitiert als das, was sie ist: ein Vorschlag ohne Eigentümer.
  (5302132001 closing.)
- **Deferred, as named successors:** the ⌘K command palette and the multi-run
  tile wall — weight without carrying power while there is one kind of run and a
  handful of objects. Bringing them back later costs nothing. (5302132001.)
- **The absorption order of the three parts is a plan, not a rule**: graph view
  with ephemeral headless tiles first, then local interactive attach, then
  remote attach as its own epic. The substrate distance is named honestly — ADR
  0002 is a single-successor chain today, and the graph needs fan-out and fan-in
  first. (#9 body @ 36800d6e.)

## Acceptance

This section reads the `Beweis` fields. It does not bind a sentence a second
time. Where the two disagree, `Beweis` is the owner. (The previous text here
said no story had declared a sentence; that was already false when #131 bound
REQ-UI-01 and REQ-UI-16 — #9 comment 5324914874.)

Declared:

- REQ-UI-01: `the-workshop-opens-in-the-studio`,
  `the-inbox-names-every-run-that-waits-for-a-human`,
  `every-level-names-the-way-back-up`,
  `the-deepest-level-shows-the-whole-way-it-sits-on`,
  `a-level-opens-from-a-pasted-link-and-survives-a-reload`
- REQ-UI-16: `an-empty-area-names-the-one-next-action`
- REQ-UIQ-04: `core-surfaces-render-owned-display-strings-under-a-pseudo-locale`
- REQ-UIQ-05: `core-surfaces-have-no-unnamed-axe-violations`
- REQ-UIQ-06: `the-studio-preserves-confirmed-truth-and-retries-only-its-failed-read`, `the-project-preserves-confirmed-truth-and-retries-only-its-failed-read`, `new-run-preserves-workflow-truth-and-retries-only-the-workflow-read`

Every other rule on this document is `UNGEBUNDEN`. The testable forms the
previous text listed as candidates — a node demanding undeclared interactive
mode, an opened tile that does not mark the run, no raw provider frame after a
live watch, a replay button that enumerates what it replays, a viewer role with
no start button — remain that: still `UNGEBUNDEN` on REQ-UI-07, REQ-UI-08,
REQ-UI-09, REQ-UI-10, and REQ-UI-17.
