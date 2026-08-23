# Requirement 0003: Ziel-UI — eine Werkstatt, vier Flächen, ein Graph

## Intent

Der Operator sieht jeden Lauf als lebenden Graphen — event-sourced, gestreamt —
mit Attempts optional als ephemeren Kacheln, die mit dem Attempt entstehen und
enden, nie als Dauersitze; Eingreifen nutzt die native Interaktivität der
Provider-Konsole, nichts davon wird nachgebaut (Issue #9 body). Um diesen Kern
sitzt die Ziel-UI nach Mockup v5: eine Werkstatt, geordnet durch eine Rail mit
vier Flächen — Chat, Board, Workflows, History — unter dem Projekt als Kontext
(Operator-Ruling 22.08.2026, Epic #516). Mockup v5 ist der Gestalt-Owner.

Der Maßstab des Operators, wörtlich (5301898411): „simple — ich kann alles
machen was ich will — alles einstellen wenn ich will. Intuitiv und es muss
Spaß machen und cool aussehen. Und natürlich alles funktionieren." Sein
Qualitätsvertrag, wörtlich (Issue #336 body): „klar strukturiert, intuitiv,
einfach zu bedienen, kein unnützer Schnickschnack, ich muss schnell finden was
ich brauche — es darf trotzdem geil aussehen und Spaß machen." Das
Zulassungskriterium für alles Weitere: es muss Arbeit wegnehmen, nicht
Features hinzufügen (5302788411).

## Rules

### REQ-UI-01: Die Werkstatt ordnet sich in eine Rail mit vier Flächen — Chat, Board, Workflows, History — samt Projekt-Umschalter und Settings/Profile-Slot; das Projekt ist der Kontext über allen vieren, kein fünfter Reiter.
Quelle: OPERATOR — Operator-Ruling 22.08.2026 („ja du hast mein Wort! alles anpassen entsprechend!", Epic #516), löst die Drei-Ebenen-Ordnung aus 5302769095 ab

### REQ-UI-02: Jeder Bildschirm beantwortet genau eine Frage.
Quelle: DESK — 5302788411 (Regel 2)

### REQ-UI-03: Workflows entstehen agentisch, nie in einem Baukasten oder Editor; ein neuer Entwurf erscheint als Karte auf dem Board, und der Operator segnet ihn dort ab.
Quelle: OPERATOR — Operator-Ruling 22.08.2026 (Epic #516), löst die Baukasten-Tür aus 5301898411 §2 ab

### REQ-UI-04: Woran die Flotte arbeitet, ist erstklassig: jede Fläche ist projekt-gescoped, und der Projekt-Umschalter in der Rail ist die eine Naht zum Projektwechsel.
Quelle: DESK — 5301898411 §1; projekt-gescopte Flächen und Umschalter-Naht per Operator-Punkt 22.08.2026 (Epic #516)

### REQ-UI-05: Die Bibliothek zeigt Namen, nie Hashes.
Quelle: DESK — 5301898411 §3

### REQ-UI-06: Derselbe Graph ist die Live-Sicht: ein Renderer in drei Zeitformen — still, live, eingefroren.
Quelle: DESK — 5301898411 §4, 5302066517 §2; drei Zeitformen per Mockup v5 (Operator-Ruling 22.08.2026)

### REQ-UI-07: Der Modus ist eine Fähigkeits-Erklärung.
Quelle: DESK — #9 body @ 36800d6ecd5d3e8922028425835b368b42d163098e5d32da930e40d25f49ce99, Kommentar 5294316639

### REQ-UI-08: Interaktives Attach ist in V1 nur lokal, und es markiert den Lauf.
Quelle: DESK — #9 body @ 36800d6ecd5d3e8922028425835b368b42d163098e5d32da930e40d25f49ce99 Teil 2, 5302132001 §2 Schluss

### REQ-UI-09: Die Live-Kachel liest den ephemeren Runner-Kanal; rohe Provider-Frames erreichen nie ein Ereignis oder ein Receipt.
Quelle: DESK — 5302132001 §2

### REQ-UI-10: Abgeschlossene Läufe bekommen Historien-Wiedergabe, keine byte-genaue.
Quelle: DESK — 5302132001 §1

### REQ-UI-11: V1 baut Projekt-Gedächtnis; Gedächtnis je Benutzer hängt an #82.
Quelle: DESK — 5302132001 §3

### REQ-UI-12: Die UI-Sprache ist komplett Englisch, mit kurzen Namen.
Quelle: OPERATOR — Operator-Ruling 22.08.2026 (Epic #516), löst „Englisch als Vorgabe, Deutsch optional" aus 5302066517 §1 ab

### REQ-UI-13: Die Run-Liste zeigt Zweck und Ergebnis in Klartext, nie nur Status.
Quelle: DESK — 5302066517 §4; Klartext-Ergebnis per Mockup v5 (Operator-Ruling 22.08.2026)

### REQ-UI-14: Ein austauschbares Design-Token-System: Struktur ist strikt von Skin getrennt, und Komponenten konsumieren ausschließlich Tokens.
Quelle: DESK — 5302066517 §5, geschärft durch Operator-Ruling 22.08.2026 (Epic #516, Token-Schicht)

### REQ-UI-15: Einstellungen sind eine professionelle Fläche ohne hartkodierte Provider-Zeilen.
Quelle: DESK — 5302066517 §6, auf den Operator-Satz „wie sie aufgebaut ist mag ich noch nicht"

### REQ-UI-16: Die Werkstatt nimmt Arbeit weg: lehrende Leerzustände, das Receipt als Schmuckstück, Rückgängig statt Nachfragen; Puls-Kopfzeile und Posteingang gehen im Board auf.
Quelle: DESK — 5302788411; Puls-Kopfzeile und Posteingang gehen im Board auf per Operator-Ruling 22.08.2026 (Epic #516)

### REQ-UI-17: Authentifizierung erhöht die Komplexität keines Bildschirms.
Quelle: DESK — 5302788411 (Regel 17)

### REQ-UI-18: Mockups sind Entwurfs-Vorlagen; der aktuelle Stand der Vorlage ist [Mockup v5](0003-ziel-ui-mockup-v5.html), regeneriert vom lebenden Original des Operators, nie unabhängig editiert.
Quelle: DESK — 5302769095, 5302066517 Schluss; Vorlage v5 abgesegnet 22.08.2026 (PR #517, Operator-Ruling 22.08.2026)

### REQ-UI-19: Atelier 1 wird als Konzepte und Lehren wiederverwendet, nie als portierter Code.
Quelle: DESK — #9 body @ 36800d6ecd5d3e8922028425835b368b42d163098e5d32da930e40d25f49ce99 (Regel 19)

### REQ-UIQ-01: Jedes Element beantwortet eine benennbare Nutzerfrage.
Quelle: DESK — #336 body @ 92d5e087748fb22ce6b01fd3a5918bd386e6dd8a80f1699105b67ce44198f9a8, Kriterium 1

### REQ-UIQ-02: Kernaufgaben erreichen ihr Ziel in einem benannten Klick- und Blick-Budget.
Quelle: DESK — #336 body @ 92d5e087748fb22ce6b01fd3a5918bd386e6dd8a80f1699105b67ce44198f9a8, Kriterium 2

### REQ-UIQ-03: Begriffe einer Fläche kommen aus einer Quelle.
Quelle: DESK — #336 body @ 92d5e087748fb22ce6b01fd3a5918bd386e6dd8a80f1699105b67ce44198f9a8, Kriterium 3

### REQ-UIQ-04: Anzeige-Strings einer Fläche kommen aus ihrem Owner, und das Layout verträgt die längere Form; Kernflächen sind die vier Rail-Flächen und die Run-Sicht.
Quelle: DESK — #336 body @ 92d5e087748fb22ce6b01fd3a5918bd386e6dd8a80f1699105b67ce44198f9a8, Kriterium 4; Kernflächen-Liste per Operator-Ruling 22.08.2026 (Epic #516)

### REQ-UIQ-05: Die Kernflächen erfüllen WCAG 2.2 AA, oder der Verstoß trägt ein Item.
Quelle: DESK — #336 body @ 92d5e087748fb22ce6b01fd3a5918bd386e6dd8a80f1699105b67ce44198f9a8, Kriterium 5

### REQ-UIQ-06: Leer, lädt, Fehler und wartet sind vier benannte Zustände.
Quelle: DESK — #336 body @ 92d5e087748fb22ce6b01fd3a5918bd386e6dd8a80f1699105b67ce44198f9a8, Kriterium 6

### REQ-UIQ-07: Eine Frage hat ein Muster, und das Muster ist eine wiederverwendete Komponente.
Quelle: DESK — #336 body @ 92d5e087748fb22ce6b01fd3a5918bd386e6dd8a80f1699105b67ce44198f9a8, Kriterium 7

### REQ-UIQ-08: Eine Fläche, die ihr Interaktions-Budget überschreitet, ist ein Defekt.
Quelle: DESK — #336 body @ 92d5e087748fb22ce6b01fd3a5918bd386e6dd8a80f1699105b67ce44198f9a8, Kriterium 8

### REQ-UIQ-09: Die Fläche darf geil aussehen und Spaß machen; der Screenshot-Maßstab ist Mockup v5, und das letzte Wort hat der Operator.
Quelle: OPERATOR — #336 body @ 92d5e087748fb22ce6b01fd3a5918bd386e6dd8a80f1699105b67ce44198f9a8, wörtlich „es darf trotzdem geil aussehen und Spaß machen"; Maßstab v5 per Operator-Ruling 22.08.2026 (Epic #516)

## Non-goals

Kein Workflow-Editor und keine Baukasten-Tür — Workflows entstehen agentisch
(Operator-Ruling 22.08.2026). Keine Dashboards und kein
Benachrichtigungszentrum neben dem Board. Die ⌘K-Befehlspalette und die
Multi-Run-Kachelwand sind benannte Nachfolger, kein V1-Bau (5302132001).
Remote-Attach ist ein eigenes Epic hinter einer Runner-Trust-Entscheidung;
nichts backt localhost ein, aber V1 baut es nicht (Issue #9 body, Teil 3).
Technische Heimaten — Katalog-Identität, Projekt-Isolation, Attach-Weg —
besitzen die ADRs, nicht dieses Dokument.
