# Requirement 0004: Execution happens anywhere, and one trust boundary is what makes that safe

```text
Status:         AGREED
Owner-Issue:    https://github.com/FlexOr2/atelier-2/issues/21
Source-Threads: #21, #1
Distilled-From: 5300858953, 5300894378, 5302132060, 5302584358, 5302587068,
                5302590978, 5302602114, 5302961156, 5302967786, 5302447161,
                5307632389
                #21 body, sha256 over the exact served bytes — 787 bytes, last
                byte `.`, nothing appended —
                5c03ceb1d5f1b85f81ec3acc1f6dea1c72d89817929a772432b9b02fbb74a56b
Approved-By:    5307632389
```

`AGREED` as a reading (operator comment 5307632389, 16.08.2026, „ja passt"): the
operator approved that this document reads its thread faithfully. That
approval settles no direction beyond what the thread itself settles — the
paragraph below keeps saying why that caution matters here. And on this subject the status carries more weight than usual, for two
reasons.

The first is the thread's voice. #21 is an **ADR mandate**, not a wish: its body
lists what a decision record must own, and almost every comment under it is the
desk writing engineering direction. **No rule below is graded `OPERATOR`.** Every
object in this thread that looks like operator authority is one of the four
things the convention names as `DESK`: a *question* („es gibt doch GitLab-Runner
— entwickeln wir ein Konzept, das wir nicht brauchen?", „ist das der richtige
Weg? sei absolut ehrlich", and the *Operator-Nachfrage* whose standard REQ-REMOTE-15
answers), a *rejection* („das reicht ja nicht — ich kenne die Umgebung nicht"), a
rendering the thread itself marks *wörtlich sinngemäß*, or an attribution the
desk wrote around its own prose (`ERGÄNZUNG (Operator…)`). So
what follows is the desk's answer to his questions, and its binding force is
negative — make no decision now that blocks it. Not one sentence below binds him
until he rules it.

The second is that the decision record this thread commissioned **landed while
this document was being reviewed**. [ADR
0009](../decisions/0009-runner-trust.md) reached `main` as PR #78's merge
`1a88dbdd`, at head `0f6d3aca`. Every `§` reference below now names a record in
the tree rather than a draft on a branch, and the convention's precedence rule —
a requirement document never outranks a landed decision record — applies for
real: where a rule below and that record disagree, the record wins and this
document is corrected.

Its own `Status` is still `PROPOSED`, decision-only, nothing implemented
(5300894378), and that is a statement about code and not about authority. What
it does mean is that no rule below may be read as built.

`Source-Threads` names `#1` for one object only: the house rule 5302447161,
which the #21 thread invokes by id at REQ-REMOTE-04. This document does not distil #1
and takes no position on its content beyond quoting the sentence the thread
leans on.

**The #21 body digest is over the exact served bytes**, reproduced with

```console
$ gh api repos/FlexOr2/atelier-2/issues/21 --template '{{.body}}' | sha256sum
```

which writes the field and appends nothing. The recipe this directory's README
currently prescribes, `--jq '.body' | sha256sum`, digests the body plus the
newline `gh`'s raw-string output adds and yields
`81cf4b1f4703ad6f7000836037beccf848fea9617c633ccd0b8a32b17dca47cf` for the same
787 bytes — an identity over a byte the object does not contain. The exact form
is used here for one reason that is not a preference: the now-landed ADR 0009
binds #21 as its decision authority by `5c03ceb1…`, and one object cited under
two digests in two documents is exactly the ambiguity a digest exists to remove.
Die Regel, die diese Form kanonisch macht, ist inzwischen **gelandet** — ADR
0010 Decision 5, PR #81 gemergt am 2026-08-15 —, und die README trägt die
Korrektur bereits: sie schreibt `gh api … --template '{{.body}}' | sha256sum`
und benennt den Zeilenumbruch, den `--jq '.body'` anhängt. Der Absatz bleibt
stehen, damit ein Leser, der eine ältere Zitation nachrechnet und den anderen
Wert erhält, weiß, welches Byte sich unterscheidet — nicht mehr als offene
Konvention.

## Intent

What the operator wants of remote execution, as 5302584358 records it — and that
comment marks its own quotation **wörtlich sinngemäß**, a rendering rather than
a transcript, so it is repeated with that qualifier and never promoted:

> Das Atelier ist Server und Koordinator; WO ausgeführt wird, ist beliebig —
> lokaler PC, CI, Docker-Sandbox, beliebiger Server. Beispiel: Codex-Runner
> lokal beim Operator, Claude-Reviewer in einer Cloud. Flexibel und sicher;
> Provider-Credentials (API-Key im Env o. ä.) liegen auf der jeweiligen
> Maschine.

Under that sit three fragments the thread attributes to him directly. **None of
them states a rule.** Two are questions and one is a rejection; they are recorded
here because they are what forced the rules below into existence, and the answers
are the desk's:

- „professionell und zugleich absolut sicher und funktionsfähig" (5302587068) —
  the standard the desk was challenged to meet. The thread frames it as an
  *Operator-Nachfrage*, and the words are not a sentence: no subject, no verb,
  nothing that can independently state a `must`. The rule holding this standard
  is the desk's reading of it; REQ-REMOTE-15.
- „es gibt doch GitLab-Runner — entwickeln wir ein Konzept, das wir nicht
  brauchen?" (5302590978) — the question that produced the build-versus-consume
  boundary. The boundary is the desk's; REQ-REMOTE-04.
- „das reicht ja nicht — ich kenne die Umgebung nicht; wo gebe ich Credentials
  an? ist das durchdacht?" (5302967786) — the rejection that produced the first
  open question. It says what is not enough without saying what to build, so
  everything answering it is `DESK`, and the part still missing stayed open
  rather than being invented.

The subject itself is stated by the mandate: **one boundary separates the
coordinating service from every runner, and it is the same boundary whether the
runner is this machine, a CI job, or a server across the world** (#21 body @
5c03ceb1).

## Rules

**Wo dieses Dokument verweist statt zu wiederholen.** Requirement
[0002](0002-teams-und-zugang.md) (#82) liest den Nachbarfaden bereits, und diese
Regeln tragen ihn nicht noch einmal:

- **Operator-Authentifizierung.** Der Rumpf von #21 verlangt sie für Cockpit und
  API aus der Ferne; die Regeln, die sie beantworten, sind REQ-ZUGANG-04 und
  REQ-ZUGANG-05 (Loopback braucht keinen Login, ein Nicht-Loopback-Bind macht ihn
  zur Pflicht, ein Mensch wird durch Login plus gewährte Rolle zugelassen).
- **Einschreibung als Zulassung einer Maschine, und der Eigentümer-Bereich auf
  einem Runner mit persönlichem Abo-Credential** — REQ-ZUGANG-06 und
  REQ-ZUGANG-10. Was #21 hinzufügt, steht unten in REQ-REMOTE-05, -06 und -14.
- **Credentials bleiben Referenzen und werden nie transportiert** —
  REQ-ZUGANG-14. #21 fügt hinzu, wo die beiden Hälften liegen und was geschieht,
  wenn eine fehlt: REQ-REMOTE-12 und -13.
- **Entfernte Maschinen sind für Abo-Credentials erstklassig** — REQ-ZUGANG-11
  entscheidet das Prinzip. REQ-REMOTE-14 trägt nur den Mechanismus.
- **Geld und Schätzungen.** REQ-ZUGANG-15 besitzt das Thema, und es ist die eine
  `OPERATOR`-Regel drüben. Nichts in diesem Dokument rechnet, zeigt oder tort auf
  einer Kostenzahl, und nichts hier wiederholt jene Regel.

Die Gruppen, in denen der Faden diese Regeln ordnete, stehen weiter in ihrer
Reihenfolge: die Grenze (01–04), was eine entfernte Maschine ist (05–06),
Platzierung (07–11), Credentials (12–14), der zu erreichende Standard (15–18),
Akteur und Kanal (19–20), die durable Hälfte im Code (21–22), Reihenfolge
(23–24).

### REQ-REMOTE-01: Ein Runner liefert Evidenz; er schreibt nie Wahrheit.
Status:     DRAFT
Quelle:     DESK — 5302584358 §3 (Regel 1), das sie als Invariante 1 des Records benennt, aus dem ADR 0009 wurde; bestätigt durch 5302602114 §2, wo sie die billigere Alternative erledigt hat
Begründung: Der koordinierende Dienst besitzt den durablen Datensatz; ein Runner besitzt den Provider-Aufruf und meldet, was er beobachtet hat. Das ist der Satz, von dem jede andere Regel hier ein Sonderfall ist.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-02: Store-Sharing wurde geprüft und verworfen, und es bleibt verworfen.
Status:     DRAFT
Quelle:     DESK — 5302602114 §2 (Regel 2)
Begründung: Eine entfernte Maschine als zweiten durablen Arbeiter gegen dieselbe Datenbank laufen zu lassen, braucht überhaupt kein eigenes Protokoll — und wurde trotzdem abgelehnt: der Runner könnte dann Wahrheit schreiben statt Evidenz zu liefern, was REQ-REMOTE-01 auslöscht. Benanntes Urteil: die größte Vertrauensfläche für die kleinste Ersparnis.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-03: Runner holen; das Atelier schiebt nie.
Status:     DRAFT
Quelle:     DESK — 5302584358 §1, 5302590978 (Regel 3)
Begründung: Runner verbinden sich ausgehend zum Atelier, holen gebundene Attempts und streamen Beobachtungen — so öffnet keine entfernte Maschine einen eingehenden Port. Transport ist ausgehendes HTTPS gegen die bestehende API, kein neues Protokoll. Ein CI-Runner ist der Einmal-Fall derselben Sache: einen holen → ausführen → melden → beenden. Das Muster wird von GitHub-Actions- und GitLab-Runnern konsumiert, nicht erfunden.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-04: Gebaut wird nur das dünne Evidenz-Protokoll; alles andere wird konsumiert.
Status:     DRAFT
Quelle:     DESK — 5302590978 (Regel 4). Das ist die Antwort auf die Operator-Frage „es gibt doch GitLab-Runner — entwickeln wir ein Konzept, das wir nicht brauchen?", und die Frage ist zitiert statt gegradet, weil sie sagt, was er bezweifelt, nicht was zu bauen ist. Der Faden ruft hier die Hausregel 5302447161 an, deren Operator-Satz lautet „Ich will nichts machen, was der Provider (Claude/Codex) mitliefert und besser kann — ich will das Drumherum verbessern."
Begründung: Vier strukturelle Gründe, warum ein CI-Runner allein diesen Vertrag nicht tragen kann — als Struktur gesagt, nicht als Geschmack: CI ist per Entwurf at-least-once (ein Infrastrukturfehler startet den Job neu), während unser Kern bei bezahlten Aufrufen at-most-once mit durablem Zeugen ist; Logs und Artefakte sind kein Receipt und keine Attestierungskette; CI bietet keine Live-Beobachtung und keinen Eingriff mitten im Lauf; und interaktive Abo-Credentials passen nicht ins CI-Secret-Modell. Also bauen wir einen kleinen Runner, der die Evidenz-Seite spricht — Attempt holen → bezeugen → Beobachtungen streamen → Receipt-Evidenz — und konsumieren den Rest: Flottenverwaltung, Bereitstellung und Skalierung von CI-Plattformen oder schlicht systemd und Docker; Identität aus Standard-mTLS; Secrets als Referenzen in lokale Quellen. Unser Runner soll *innerhalb* eines GitLab- oder GitHub-Runners im Einmal-Modus laufen, und der erste Fernbeweis ist genau das — unser Runner in einem GitHub-Actions-Job, ohne eigene Infrastruktur. Der zitierte Hausregel-Satz handelt von provider-mitgelieferter Fähigkeit; ihn auf CI-Plattformen und PKI zu verallgemeinern ist die Verallgemeinerung des Desks, weshalb diese Regel `DESK` ist und nicht `OPERATOR`.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-05: Eine entfernte Maschine braucht genau vier Dinge, und kein fünftes.
Status:     DRAFT
Quelle:     DESK — 5302584358 §2 (Regel 5)
Begründung: Das Runner-Binary oder den Container; die Einschreibung (ein Einmal-Token, getauscht gegen ein Credential je Runner, gegenseitiges TLS); das Provider-Credential **lokal**, per Referenz; und eine attestierte Sandbox-Fähigkeit. Was eine fünfte Anforderung hinzufügen würde, ist die offene Frage am Ende dieses Dokuments, keine stille Ergänzung hier.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-06: Die Einschreibung gilt je Runner, und das Register beantwortet „welche Runner sind meine".
Status:     DRAFT
Quelle:     DESK — 5302584358 §2; 5302961156 §1 für `owner`+`allowed-projects` als abgeglichene Fakten; REQ-ZUGANG-10 für das Register (Regel 6)
Begründung: REQ-ZUGANG-10 besitzt das Register selbst; was dieses Thema hinzufügt, ist, dass der Einschreibe-Datensatz der einzige Ort ist, an dem Identität eines Runners, sein Eigentümer und seine erlaubten Projekte zu *Fakten werden, auf die das Atelier abgleichen kann* (REQ-REMOTE-07), statt Behauptungen zu bleiben, die ein sich verbindender Prozess über sich selbst aufstellt. Ein nicht eingeschriebener Runner ist kein Runner; eine widerrufene Einschreibung trifft genau eine Maschine.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-07: „Tags" sind attestierte Fakten, nie getippte Labels.
Status:     DRAFT
Quelle:     DESK — 5302961156 §1 (Regel 7)
Begründung: Runner-Auswahl ist der Abgleich der Anforderungen einer Bindung gegen **bewiesene** Fähigkeiten: auflösbare Credential-Referenzen, Sandbox-Stufe, Provider-Versionen, `owner` und `allowed-projects`. Die Ergonomie mag wie GitLab-Tags aussehen; die Substanz ist Attestierung, denn ein getipptes Label ist eine Behauptung, und Behauptungen sind genau das, was diese Grenze abweisen soll. Manuelle Runner-Klassen-Einschränkungen je Rolle sind zusätzlich erlaubt, und sie ersetzen den Abgleich nie — sie verengen ihn.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-08: Anforderungen werden an der Arbeit erklärt, Fähigkeiten vom Runner attestiert, und das Atelier gleicht nur ab.
Status:     DRAFT
Quelle:     DESK — 5302967786 (Regel 8), das diese Hälfte als entschieden ausweist
Begründung: Das zweiseitige Prinzip, mit benannter erklärender Seite: die Agenten-Definition erklärt den Werkzeugbedarf, die Executor-Revision den Laufzeitbedarf, das Projekt die Runner-Klassen und Platzierungsregeln. Das Atelier selbst erklärt nichts und attestiert nichts; es gleicht ab.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-09: Platzierung geschieht je Attempt, nie je Lauf, und ein laufender Attempt wandert nie.
Status:     DRAFT
Quelle:     DESK — 5302961156 §2 (Regel 9); im Einklang mit ADR 0009 §10
Begründung: At-most-once und die Evidenzkette sterben beide in dem Moment, in dem ein lebender Attempt umzieht. Ein verlorener Runner ergibt `POSSIBLY_RAN`, laut, und nie eine Neuplatzierung.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-10: Maschinenwechsel geschieht an ehrlichen Grenzen, und ein Ersatz-Attempt ist erstklassig.
Status:     DRAFT
Quelle:     DESK — 5302961156 §2 (Regel 10)
Begründung: Retry, Fortsetzen und ein bewusst ersetzter Attempt werden jeder **neu platziert** — automatisch auf jeden attestierend passenden Runner oder vom Operator festgesetzt. Der Normalfall des Fadens selbst: dem Runner gingen die Token aus, also läuft der nächste Attempt auf einer anderen Maschine, als **neuer quittierter Attempt** statt als Fortsetzung des alten. Ersatz-Attempts werden erstklassig gebaut, nicht als Fehlermodus modelliert.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-11: Platzierung scheitert geschlossen.
Status:     DRAFT
Quelle:     DESK — 5302587068, „fail-closed-Platzierung (Arbeit wartet sichtbar statt unsicher zu laufen)"; 5302584358 §4 (Regel 11)
Begründung: Arbeit wartet sichtbar, statt unsicher zu laufen; eine Bindung, die kein verbundener, eingeschriebener Runner attestiert, bekommt keinen Runner, der beinahe passt, und kein Modus wird still herabgestuft. Bis der offene Kern am Ende dieses Dokuments entschieden ist, bleiben **Fernbindungen** überhaupt **verweigert**. Was „wartet sichtbar" in der Maschine heißt, muss dieses Dokument nicht mehr raten: ADR 0009 §7, gelandet, entscheidet, dass ein nicht platzierbarer Lauf beim Start verweigert und nie eingereiht wird, und die offene Frage unten hält dieses Ruling fest statt der Uneinigkeit, die es ersetzt hat.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-12: Werte leben auf der Maschine; Namen leben in der Konfiguration.
Status:     DRAFT
Quelle:     DESK — 5302967786; REQ-ZUGANG-14 besitzt die Nie-transportiert-Hälfte (Regel 12)
Begründung: Beide Orte sind richtig, und sie sind verschiedene Dinge: der Credential-**Wert** sitzt auf dem Host — Umgebung, Datei, Schlüsselbund —, bei der Einschreibung dorthin gelegt; der **Name oder die Referenz** sitzt in der Konfiguration, wo ein Projekt sagt, es benutze Referenz `github-work`. Wer diese Referenz auflösen kann, ist ein attestierter Fakt (REQ-REMOTE-07), nie ein Transport. Das Atelier ist zu keinem Zeitpunkt ein Secret-Verteilkanal.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-13: Ein fehlendes Credential scheitert geschlossen auf drei Schichten, und keine Schicht stuft herab.
Status:     DRAFT
Quelle:     DESK — Schichten (b) und (c) sind 5302961156 §1 und 5302584358 §2/§3, und ADR 0009 §6/§7 gibt ihnen die Verweigerungsnamen `auth-profile-unresolvable` und `no-runner-attests-binding`, die nun binden, da der Record gelandet ist (Regel 13)
Begründung: (a) Die *Konfiguration* trägt nur eine Referenz, sodass ein fehlender Wert nicht als Wert eingeschmuggelt werden kann. (b) Die *Platzierung* verweigert eine Bindung, von der kein verbundener, eingeschriebener Runner attestiert, sie auflösen zu können — die Auflösbarkeit der Referenz ist einer der abgeglichenen Fakten von REQ-REMOTE-07, sodass eine unauflösbare Bindung nie eine Maschine erreicht. (c) Der *Laufstart auf dem Runner* verweigert, wenn die gebundene Referenz auf diesem Host nicht auflöst, ohne Rückfall auf einen anderen Auth-Modus. Jede Schicht verweigert; keine setzt ein schwächeres Credential oder einen anderen Modus an die Stelle.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-14: Entfernte Abo-Runner sind in der Praxis erstklassig, nicht nur im Prinzip.
Status:     DRAFT
Quelle:     DESK — 5302584358, dessen Zitat der Faden als *wörtlich sinngemäß* markiert; 5302961156 §1 für die auflösbare Credential-Referenz als abgeglichenen Fakt (Regel 14)
Begründung: REQ-ZUGANG-11 stellt das Prinzip auf, selbst als `DESK`: eine langlebige entfernte Maschine mit einem interaktiven Login trägt einen Abo-Executor vollständig, und nur *ephemere* Umgebungen können es strukturell nicht. Was dieses Thema besitzt, ist, was das auf einer echten Maschine wahr macht — REQ-REMOTE-05, -06, -12 und -13 — und die Folge, dass ein entfernter Abo-Runner ein gewöhnliches Platzierungsziel ist, keine in den Abgleicher geschnitzte Ausnahme. Das Bild dieses Fadens vom Endzustand hat genau diese Form: ein Codex-Runner lokal beim Operator, ein Claude-Reviewer in einer Cloud, mit den Provider-Credentials auf ihren jeweiligen Maschinen.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-15: Der Endzustand ist professionell und zugleich absolut sicher und funktionsfähig; Sicherheit wird nicht mit Unbenutzbarkeit erkauft.
Status:     DRAFT
Quelle:     DESK — 5302587068 (Regel 15), das den Standard so rahmt:

            > „professionell und zugleich absolut sicher und funktionsfähig"
Begründung: Eine frühere Fassung gradete diese Regel `OPERATOR`. Das war falsch, und der Grund, warum es falsch war, gehört an die Regel. Der Grad der Konvention verlangt, dass das zitierte Objekt den **Satz** des Operators wiedergibt und dass dieser Satz sagt, was die Regel sagt. Das scheitert an beiden Hälften. Der Kommentar rahmt die Worte als *Operator-Nachfrage* — eine Frage, die die Konvention rundweg `DESK` gradet — und tut das innerhalb einer vom Desk geschriebenen `ERGÄNZUNG (Operator…)`-Zuschreibung, die die Konvention ebenfalls `DESK` gradet. Die Worte selbst sind ein Fragment: kein Subjekt, kein Verb, also können sie das `muss` dieser Regel nicht eigenständig aufstellen. Sie als autoritativ zu lesen, weil sie kein Fragezeichen enthalten, ließe die Stimmprüfung an Zeichensetzung innerhalb einer desk-geschriebenen Rahmung hängen — genau die Selbstbeglaubigung, die die Konvention abweisen soll. Keines der zehn Objekte, die dieses Dokument destilliert, enthält einen vollständigen Operator-Satz, der diese Anforderung aufstellt; wird später einer gepostet, wird diese Regel daran neu gegradet. Das Fragment bleibt, weil es der Standard ist, der die Regel motiviert hat — drei Begriffe, zusammengehalten durch *zugleich* —, und der Rest des Kommentars ist Desk-Ausarbeitung, die ebenfalls nichts bindet: die vierstufige Reifeleiter, die Wahl von SPIFFE/SPIRE als Endform und das Ein-Befehl-Onboarding von REQ-REMOTE-16. Die Rahmung „Long-term, nicht Prio 1" steht ebenso außerhalb der Anführungszeichen und ist die des Desks, weshalb REQ-REMOTE-23 sie als Reihenfolge des Desks trägt und nicht als seine Anweisung.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Wird ein vollständiger Operator-Satz zu diesem Standard gepostet, wird die Regel daran neu gegradet (Eigentümer: Operator, Ziel: Ruling)

### REQ-REMOTE-16: Onboarding ist ein Befehl, und alles danach ist unsichtbar korrekt.
Status:     DRAFT
Quelle:     DESK — 5302587068 (Regel 16)
Begründung: `atelier runner join <token>` und sonst nichts, weil Sicherheit mit Reibung umgangen wird und unsichtbare Sicherheit gelebt wird. Das ist die betriebliche Lesart des Desks von *funktionsfähig* aus REQ-REMOTE-15.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-17: Keine eigene PKI und kein eigenes Identitäts-Framework.
Status:     DRAFT
Quelle:     DESK — 5302587068 (Regel 17)
Begründung: Standard-mTLS-Werkzeug wird jetzt konsumiert, Workload-Identität nach SPIFFE/SPIRE ist die benannte Endform (automatisch rotierende kurzlebige Identitäten statt langlebiger Secrets), und die Konvergenz ist beabsichtigt: das in-toto-Agent-Prädikat aus der #104-Recherche benutzt SPIFFE-IDs, sodass Dossier und Runner-Identität am Ende eine Sprache sprechen. Menschen authentifizieren sich per OIDC, was das Thema von 0002 ist und nicht dieses.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-18: Drei Prinzipien gelten über jede Stufe der Leiter.
Status:     DRAFT
Quelle:     DESK — 5302587068, mit 5302590978 dafür, dass Stufe 2 das Runner-Protokoll ohne die Leitung ist (Regel 18)
Begründung: Zero Trust — Identität plus Attestierung, nie Netzwerkposition; kurzlebig statt widerrufbar; und fail-closed-Platzierung, die REQ-REMOTE-11 vollständig aufstellt. Die Leiter, über die sie gelten: Stufe 1 lokal, nur Loopback (erledigt), Stufe 2 Prozesstrennung des Betriebssystems auf einem Host (#15, in Arbeit — die Miniatur des Fernfalls mit denselben Invarianten und ohne Netz), Stufe 3 holende Runner mit mTLS-Einschreibung, Lease/Heartbeat/Fencing und attestierter Sandbox, Stufe 4 Workload-Identität. Die Leiter ist Richtung, kein Zeitplan.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-19: Jeder Befehl trägt einen typisierten, authentifizierten Akteur.
Status:     DRAFT
Quelle:     DESK — #21 body @ 5c03ceb1; 5300894378 für den Befund des selbstbehaupteten Labels (Regel 19)
Begründung: Und solange er das nicht tut, darf nichts Zurechnung genannt werden. Das Mandat benennt Akteurs- und Zurechnungsmodell für Befehle als ADR-Pflicht (mit Zufluss zu #7); die ehrliche Hälfte ist, dass der heutige Reconcile-Akteur ein selbstbehauptetes Label ist, weshalb keine Oberfläche ihn als Zurechnung darstellen darf.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-20: Der Terminal-Kanal ist eine getrennt getorte, standardmäßig ausgeschaltete Fähigkeit.
Status:     DRAFT
Quelle:     DESK — #21 body @ 5c03ceb1; 5300894378. Die allgemeine Form der Prüfspur ist REQ-ZUGANG-03 (Regel 20)
Begründung: Mit Step-up je Attach, kurzlebigen Token und einer Attach-Prüfspur. Es ist der eine Kanal, der die Tastenanschläge eines Menschen in einen credential-tragenden Prozess trägt, weshalb Ausführungsfähigkeit ihn nie impliziert.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-21: „Entscheidung und Einreihung sind eine Transaktion" muss erzwingbar werden statt handgeschrieben.
Status:     DRAFT
Quelle:     DESK — 5302132060, Befund H4, HIGH, gegen main `b9c7796e` (Regel 21)
Begründung: Die Invariante von ADR 0001 ist an jeder Aufrufstelle handgeschrieben — die Überschrift des Befunds sagt fünf, die Liste darunter nennt sieben, über fünf Module —, jede mit eigenen Vor- und Nachbedingungen, sodass eine weitere Stelle, die außerhalb der Transaktion einreiht, sie verletzt, ohne dass ein Tor oder ein Test es bemerkt. Genau diesen Zustand muss diese Grenze ausschließen: einen Runner, der einen Attempt empfängt, den keine committete Entscheidung deckt. Benannte kleinste Behebung: ein Eigentümer, der Entscheidungs-Schreibung und Einreihung in einem Aufruf nimmt, damit „Entscheidung ohne Einreihung" ein Typfehler wird statt einer Review-Frage.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-22: „Welche Executor-Modi kann dieser Runner erzwingen" braucht genau einen Autor.
Status:     DRAFT
Quelle:     DESK — 5302132060, Befunde M7 und M8, MED (Regel 22)
Begründung: Heute hat die Antwort zwei: eine zweite Kompositionswurzel baut die Executor-Registry aus Factory-Listen parallel zum Anwendungs-Komponisten (M8), und die Registry selbst trägt Auflösungsverhalten im laufzeitreinen Ports-Paket, wo ein optionales Argument still eine leere Registry baut — eine fail-open-Vorgabe, die nur existiert, um Tests billiger zu machen (M7). Beides ist für dieses Thema derselbe Defekt: eine Vertrauensgrenze, deren zentrale Frage zwei Autoren hat, kann nicht attestiert werden.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-23: Das Tor zum Bau der Ferne ist Wert, kein Kalender.
Status:     DRAFT
Quelle:     DESK — 5302602114 §1 (Regel 23)
Begründung: Für den Einzeloperator-Betrieb bleibt der Wert der Ferne lange bescheiden, also wird sie beim ersten echten Zweitmaschinen-Bedarf gebaut — der benannte Kandidat ist das „von der Arbeit aus"-Szenario von #79 — und „nicht Priorität eins" ist Teil davon, richtig zu sein, keine Ausrede.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-24: Verwaltete Provider-Sandkästen sind ein Beobachtungspunkt, kein Wettbewerber, gegen den gebaut wird.
Status:     DRAFT
Quelle:     DESK — 5302602114 §3 (Regel 24)
Begründung: Reift die Ausführung in der Cloud eines Providers unter Operator-Richtlinien, schrumpft unser Runner für diese Fälle zu einem Adapter, und die Platzierungsnaht — attestierte Fähigkeit — überlebt unverändert; selbst gehostet bleibt der Souveränitätspfad. Die Anweisung lautet, zu beobachten und die Naht zu halten, nicht eine der beiden Seiten vorzubauen.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

## Open questions

- **The environment-requirements vocabulary is missing, and this epic owes it
  before any remote release.** The operator's rejection is the source: „das
  reicht ja nicht — ich kenne die Umgebung nicht; wo gebe ich Credentials an?
  ist das durchdacht?" (5302967786). What is decided is the *structure* —
  declare, attest, match (REQ-REMOTE-08) and the two credential places (REQ-REMOTE-12). What
  is missing is the *content*: the concrete requirement vocabulary beyond
  credentials, sandbox level, and provider version — tools (`docker`,
  `node@20`), resources (RAM, disk, GPU), network reachability — **each with its
  form of proof**, since the honest question is how a runner attests "has
  Docker" without merely claiming it (a version probe, not an assertion). The
  thinking model is to be consumed rather than invented: Kubernetes selectors
  and taints, GitHub Actions `runs-on`. The extension seam exists — ADR 0006's
  capability vocabulary is versioned and extensible and ADR 0009 §7's
  attestation takes new entries — so what is undecided is the content, not
  the structure. This is an open building block, deliberately not written as a
  rule, because a document that resolved it would be inventing the requirement
  it is supposed to be reading. Owner: this epic, before remote is released.

- **The named open core before remote release** (5302584358 §4): the lease,
  heartbeat, and fencing contract; the transport; and the runner's packaging and
  update channel. Until these are decided, REQ-REMOTE-11 keeps remote bindings
  refused. Owner: the remote ADR, #9 part 3.

- **Does an unplaceable binding wait or refuse? — closed by citation.** The
  question was whether the thread's „Arbeit wartet sichtbar statt unsicher zu
  laufen" (5302587068) and the ADR's refusal at run start were the same answer.
  [ADR 0009](../decisions/0009-runner-trust.md) §7, landed, rules it: an
  unplaceable run is **refused** — before any durable run, binding, attempt, or
  provider process — and "never queued in the hope a runner appears, because
  queueing it turns fail-closed into a hang". The refusal
  `no-runner-attests-binding` names the node, the binding and the missing
  attestation, so what „wartet sichtbar" wanted — the operator seeing it rather
  than a silent stall — is carried by the refusal and not by a waiting item.
  Nothing is left for this document to choose.

- **What the ADR mandate deliberately did not decide** (5300858953, 5300894378):
  transport and protocol details; the remote epic's scope and its
  attempt-ownership contract (#9 part 3); multi-project isolation (#23); and the
  sandbox mechanism (#60).

- **The secrets-channel decision is shared with #24 and must be taken once.**
  The audit's recommended order is the graph interpreter's move into the core
  (#86) first, then REQ-REMOTE-21 and REQ-REMOTE-22, then the secrets-channel decision that #21
  and #24 both need — decided once, not twice. (5302132060, closing.)

## Acceptance

No story has declared an acceptance sentence for this requirement, so what
follows is a set of candidates and not a set of declared sentences; none of
them has an identifier to name. The rules above already state in testable form, notably:

- a process that reaches the service without an enrolment receives no attempt;
- a binding that no connected, enrolled runner attests starts nothing — no
  durable run, no attempt, no provider process;
- a runner whose bound credential reference does not resolve on its host refuses
  rather than falling back to another auth mode;
- a running attempt never changes machine, and a lost runner yields
  `POSSIBLY_RAN` rather than a second placement;
- a resumed or replaced attempt appears as a new receipted attempt, on a runner
  chosen by matching and not by a typed label;
- a runner attestation that differs from the enrolled one is a visible diff
  requiring a fresh operator act, never a silent widening;
- no credential value appears in any record, projection, or transmission in
  either direction;
- attach is off unless separately enabled, and every attach is audited.

The environment-requirements gap above is deliberately absent from this list:
until that vocabulary exists there is no honest sentence to write for it, and an
acceptance sentence invented ahead of its requirement is the failure this
directory's convention exists to prevent.
