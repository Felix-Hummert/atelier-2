# Requirement 0004: Execution happens anywhere, and one trust boundary is what makes that safe

```text
Status:         DRAFT
Owner-Issue:    https://github.com/FlexOr2/atelier-2/issues/21
Source-Threads: #21, #9, #5, #1
Distilled-From: 5300858953, 5300894378, 5302132060, 5302584358, 5302587068,
                5302590978, 5302602114, 5302961156, 5302967786, 5302447161,
                5307632389, 5354196886, 5354522420, 5354779824, 5354786342
                #21 body, sha256 over the exact served bytes — 5,072 bytes,
                ending in one LF byte, nothing appended —
                afc86a8f64f39ecd6be7de5db302ffbe5319c203e2c9ac4d31d3f868b8c61fb2
Approved-By:    none
```

The preceding revision was `AGREED` as a reading under operator comment
5307632389. The #21 body and architecture boundary changed on 2026-08-20, so
that approval remains history and does not approve these new bytes. This refresh
is therefore `DRAFT` with `Approved-By: none` until the operator approves this
exact reading.

#21 is an ADR mandate and almost all of its source objects are engineering-desk
direction. Comment 5354196886 records an operator-owned architecture ruling, but
its prose is a desk attribution rather than the operator's quoted voice under
this directory's grading convention. The new rules remain `DESK`; no source is
promoted to `OPERATOR` by its label or by being posted from the operator account.

[ADR 0009](../decisions/0009-runner-trust.md) is the technical owner and remains
`PROPOSED`: the boundary is decided, not implemented. Where this derived reading
and the ADR disagree, the ADR wins. The implementation phases and acceptance
live on [#15](https://github.com/FlexOr2/atelier-2/issues/15),
[#301](https://github.com/FlexOr2/atelier-2/issues/301) and
[#312](https://github.com/FlexOr2/atelier-2/issues/312); this document does not
copy their plans.

The #21 digest above is over the exact body bytes and is reproduced with
`gh api repos/FlexOr2/atelier-2/issues/21 --template '{{.body}}' | sha256sum`.
The template appends no extra newline; the body itself ends in one LF byte. ADR
0010 owns that provenance rule.

## Intent

What the operator wants of remote execution, as 5302584358 records it — and that
comment marks its own quotation **wörtlich sinngemäß**, a rendering rather than
a transcript, so it is repeated with that qualifier and never promoted:

> Das Atelier ist Server und Koordinator; WO ausgeführt wird, ist beliebig —
> lokaler PC, CI, Docker-Sandbox, beliebiger Server. Beispiel: Codex-Runner
> lokal beim Operator, Claude-Reviewer in einer Cloud. Flexibel und sicher;
> Provider-Credentials (API-Key im Env o. ä.) liegen auf der jeweiligen
> Maschine.

Three operator fragments drove the design without choosing it: „professionell
und zugleich absolut sicher und funktionsfähig" (5302587068), „es gibt doch
GitLab-Runner — entwickeln wir ein Konzept, das wir nicht brauchen?"
(5302590978), and „das reicht ja nicht — ich kenne die Umgebung nicht; wo gebe
ich Credentials an? ist das durchdacht?" (5302967786). They are respectively a
fragment, a question and a rejection, so the engineering answers remain `DESK`.

The current mandate is one carrier-neutral boundary: Core/Serve owns durable
truth; an Atelier Runner executes one AgentAttempt and returns evidence; an
Effect Worker executes one prepared EffectIntent under a separate grant; CI or
local OCI only carries a worker. Serve and Runner are separate OCI images and
release artifacts. The concrete carrier, launch authority and mutual-
authentication mechanism remain an open operator decision on #21 (#21 body @
`afc86a8f`; 5354196886, 5354522420).

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
  REQ-ZUGANG-10. Was #21 hinzufügt, steht unten in REQ-REMOTE-06, -14 und -25.
- **Provider-Credentials bleiben Referenzen und werden nie transportiert** —
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
(23–24) und die 2026-08-20-Grenzschärfung (25–28).

### REQ-REMOTE-01: Ein Runner liefert Evidenz; er schreibt nie Wahrheit.
Status:     DRAFT
Quelle:     DESK — 5302584358 §3 und 5302602114 §2; bestätigt durch #21 body @ afc86a8f und 5354522420
Begründung: Core/Serve besitzt Scheduling und den durablen Datensatz; ein Atelier Runner besitzt genau einen gebundenen Agent-Aufruf und meldet Evidenz. Native CI-Artefakte oder Worker-Evidenz werden dadurch nicht selbst zur Wahrheit.
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
Status:     SUPERSEDED
Quelle:     DESK — 5302584358 §1 und 5302590978 (alte Regel 3); superseded durch #21 body @ afc86a8f und 5354196886
Begründung: Die alte Lesart legte ausgehendes HTTPS und Pull als Zielmechanismus fest. Der aktuelle #21-Owner lässt Carrier, Launch-Autorität und konkreten Mutual-Auth-/Transportmechanismus ausdrücklich offen. REQ-REMOTE-28 ersetzt diese Vorentscheidung durch ein Stop-Gate.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-04: Gebaut wird nur das dünne Evidenz-Protokoll; alles andere wird konsumiert.
Status:     DRAFT
Quelle:     DESK — 5302590978 und 5302447161 (alte Regel 4); bestätigt und geschärft durch #21 body @ afc86a8f und 5354522420
Begründung: Atelier baut den schmalen Worker-/Evidenzvertrag und konsumiert Carrier, OIDC und Provider-Ausführung. CI bleibt Carrier statt Scheduler oder Store of Record; native Logs und Artefakte sind Transport oder Provenienz, keine canonical artifacts oder Receipts. Der erste CI-Proof ist Agent-only; der getrennte Effect Worker folgt später. Das ist eine Desk-Antwort auf die zitierte Operator-Frage, keine Operator-Regel.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-05: Eine entfernte Maschine braucht genau vier Dinge, und kein fünftes.
Status:     SUPERSEDED
Quelle:     DESK — 5302584358 §2 (alte Regel 5); superseded durch #21 body @ afc86a8f und 5354522420
Begründung: Die Vier-Dinge-Regel nahm individuelle Einschreibung für jede Umgebung an. REQ-REMOTE-25 ersetzt sie: langlebige Runner werden einzeln eingeschrieben, ephemere CI-Jobs über eine enge TrustPolicy zugelassen.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-06: Die Einschreibung gilt je langlebigem Runner, und das Register beantwortet „welche Runner sind meine".
Status:     DRAFT
Quelle:     DESK — 5302584358 §2; 5302961156 §1; REQ-ZUGANG-10; geschärft durch #21 body @ afc86a8f
Begründung: REQ-ZUGANG-10 besitzt das Register. Bei einem langlebigen Runner macht nur der Einschreibe-Datensatz Identität, Eigentümer und erlaubte Projekte zu abgleichbaren Fakten; Widerruf trifft genau diesen Runner. Ephemere CI-Jobs sind die ausdrücklich getrennte REQ-REMOTE-25-Form und erscheinen nicht als manuell eingeschriebene langlebige Runner.
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
Quelle:     DESK — 5302961156 §2; geschärft durch #21 body @ afc86a8f und 5354522420; im Einklang mit ADR 0009 §10
Begründung: Vor `LAUNCH_ARMED` erlaubt nur autoritative No-launch-Evidenz eine neue Platzierung. Ab `LAUNCH_ARMED` ergibt ein verlorener Runner `POSSIBLY_RAN`, laut und ohne Neuplatzierung desselben Attempts.
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
Begründung: Arbeit wartet sichtbar, statt unsicher zu laufen; eine Bindung, die kein verbundener, autorisierter Runner attestiert, bekommt keinen Runner, der beinahe passt, und kein Modus wird still herabgestuft. Autorisiert heißt langlebig eingeschrieben oder ephemer durch die CI TrustPolicy zugelassen. Bis der offene Kern am Ende dieses Dokuments entschieden ist, bleiben Fern- und CI-Bindungen verweigert. ADR 0009 §7 entscheidet, dass ein nicht platzierbarer Lauf vor durablem Start verweigert und nie auf Hoffnung eingereiht wird.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-12: Provider-Credential-Werte leben auf der Maschine; ihre Namen leben in der Konfiguration.
Status:     DRAFT
Quelle:     DESK — 5302967786; REQ-ZUGANG-14 besitzt die Nie-transportiert-Hälfte (Regel 12)
Begründung: Beide Orte sind richtig, und sie sind verschiedene Dinge: der Provider-Credential-**Wert** sitzt auf dem Host — Umgebung, Datei, Schlüsselbund —; der **Name oder die Referenz** sitzt in der Konfiguration, wo ein Projekt sagt, es benutze Referenz `github-work`. Wer diese Referenz auflösen kann, ist ein attestierter Fakt (REQ-REMOTE-07), nie ein Transport. Das Atelier ist zu keinem Zeitpunkt ein Provider-Secret-Verteilkanal. Das kurzlebige Worker-Identity-Credential aus REQ-REMOTE-25 ist ein anderer Vertrag: Seine Ausgabe folgt dem einmaligen TrustPolicy-Austausch, es ist auf Attempt, Generation und Worker-Rolle begrenzt, und sein Wert erscheint in keinem dauerhaften Atelier-Zustand, Log oder Carrier-Artefakt. Transport und ephemere Repräsentation bleiben Teil der offenen Mutual-Auth-Entscheidung auf #21.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-13: Ein fehlendes Credential scheitert geschlossen auf drei Schichten, und keine Schicht stuft herab.
Status:     DRAFT
Quelle:     DESK — Schichten (b) und (c) sind 5302961156 §1 und 5302584358 §2/§3, und ADR 0009 §6/§7 gibt ihnen die Verweigerungsnamen `auth-profile-unresolvable` und `no-runner-attests-binding`, die nun binden, da der Record gelandet ist (Regel 13)
Begründung: (a) Die Konfiguration trägt nur eine Referenz, nie einen Wert oder Serve-lokalen Credential-Pfad. (b) Die Platzierung verweigert eine Bindung, die kein autorisierter Runner als auflösbar attestiert. (c) Der Runner verweigert vor Provider-Start, wenn die Referenz lokal nicht auflöst. Keine Schicht fällt auf einen anderen Auth-Modus zurück.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-14: Entfernte Abo-Runner sind in der Praxis erstklassig, nicht nur im Prinzip.
Status:     DRAFT
Quelle:     DESK — 5302584358, dessen Zitat der Faden als *wörtlich sinngemäß* markiert; 5302961156 §1 für die auflösbare Credential-Referenz als abgeglichenen Fakt (Regel 14)
Begründung: REQ-ZUGANG-11 stellt das Prinzip auf, selbst als `DESK`: eine langlebige entfernte Maschine mit einem interaktiven Login trägt einen Abo-Executor vollständig, während ephemere Umgebungen andere Credential-Grenzen haben. REQ-REMOTE-06, -12, -13 und -25 machen den langlebigen Fall gewöhnlich statt zu einer Sonderbehandlung; Provider-Credential-Werte bleiben auf der jeweiligen Maschine.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-15: Der Endzustand ist professionell und zugleich absolut sicher und funktionsfähig; Sicherheit wird nicht mit Unbenutzbarkeit erkauft.
Status:     DRAFT
Quelle:     DESK — 5302587068 (Regel 15), das den Standard so rahmt:

            > „professionell und zugleich absolut sicher und funktionsfähig"
Begründung: Eine frühere Fassung gradete das Fragment `OPERATOR`. Die Quellenkonvention verlangt jedoch einen vollständigen Operator-Satz, der diese Regel selbst ausspricht; 5302587068 rahmt das Fragment als Desk-Zuschreibung einer Operator-Nachfrage. Deshalb bleibt der Qualitätsstandard als `DESK`-Lesart erhalten, ohne eine alte SPIFFE/SPIRE-Endform, Reifeleiter oder Reihenfolge als bindend weiterzutragen. Ein späterer vollständiger Operator-Satz würde die Regel neu graden.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Wird ein vollständiger Operator-Satz zu diesem Standard gepostet, wird die Regel daran neu gegradet (Eigentümer: Operator, Ziel: Ruling)

### REQ-REMOTE-16: Onboarding ist ein Befehl, und alles danach ist unsichtbar korrekt.
Status:     DRAFT
Quelle:     DESK — 5302587068 (Regel 16)
Begründung: Für einen langlebigen Runner ist `atelier runner join <token>` der einzige sichtbare Einschreibeschritt, weil Sicherheit mit Reibung umgangen wird. Ephemere CI-Jobs nutzen stattdessen REQ-REMOTE-25 und werden nie manuell eingeschrieben. Beides ist die betriebliche `DESK`-Lesart von *funktionsfähig* aus REQ-REMOTE-15.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-17: Keine eigene PKI und kein eigenes Identitäts-Framework.
Status:     DRAFT
Quelle:     DESK — 5302587068 (alte Regel 17); geschärft durch #21 body @ afc86a8f und 5354522420
Begründung: Atelier konsumiert Standardidentität statt eine PKI oder ein CI-Identitätsframework nachzubauen. Die konkrete Mutual-Auth-Technik bleibt unter REQ-REMOTE-28 offen; OIDC ist nur für die beschriebene ephemere CI TrustPolicy entschieden, nicht als allgemeiner Transportmechanismus.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-18: Drei Prinzipien gelten über jede Stufe der Leiter.
Status:     DRAFT
Quelle:     DESK — 5302587068 und 5302590978 (alte Regel 18); ersetzt in der Bauordnung durch #21 body @ afc86a8f, 5354196886 und 5354522420
Begründung: Identität plus Attestierung statt Netzwerkposition, kurzlebige operation-scoped Autorität und fail-closed-Platzierung gelten lokal, remote und in CI. Die konkrete Baufolge liegt auf #15, #301 und #312 (`#15-A → #301-A → #15-B → #301-B → #312 → Deletion`); dieses Requirement übernimmt keine zweite Kopie ihrer Acceptance.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-19: Jeder Befehl trägt einen typisierten, authentifizierten Akteur.
Status:     DRAFT
Quelle:     DESK — #21 body @ afc86a8f; 5300894378 für den Befund des selbstbehaupteten Labels (Regel 19)
Begründung: Und solange er das nicht tut, darf nichts Zurechnung genannt werden. Das Mandat benennt Akteurs- und Zurechnungsmodell für Befehle als ADR-Pflicht (mit Zufluss zu #7); die ehrliche Hälfte ist, dass der heutige Reconcile-Akteur ein selbstbehauptetes Label ist, weshalb keine Oberfläche ihn als Zurechnung darstellen darf.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-20: Der Terminal-Kanal ist eine getrennt getorte, standardmäßig ausgeschaltete Fähigkeit.
Status:     DRAFT
Quelle:     DESK — #21 body @ afc86a8f; 5300894378. Die allgemeine Form der Prüfspur ist REQ-ZUGANG-03 (Regel 20)
Begründung: Mit Step-up je Attach, kurzlebigen Token und einer Attach-Prüfspur. Es ist der eine Kanal, der die Tastenanschläge eines Menschen in einen credential-tragenden Prozess trägt, weshalb Ausführungsfähigkeit ihn nie impliziert.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-21: „Entscheidung und Einreihung sind eine Transaktion" muss erzwingbar werden statt handgeschrieben.
Status:     SUPERSEDED
Quelle:     DESK — 5302132060, Befund H4 gegen den damaligen Stand; superseded durch #21 body @ afc86a8f
Begründung: Der alte Auditbefund lokalisierte die fehlende Invariante in handgeschriebenen Aufrufstellen und schlug bereits eine Implementierungsform vor. Der aktuelle Owner ersetzt diese Vorschrift: #15 besitzt State, Launch-Fence, terminale Evidenzannahme, ACK und Reconciliation. Dieses Requirement bewahrt nur den historischen Grund und schreibt #15 keine zweite Lösung vor.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-22: „Welche Executor-Modi kann dieser Runner erzwingen" braucht genau einen Autor.
Status:     SUPERSEDED
Quelle:     DESK — 5302132060, Befunde M7 und M8 gegen den damaligen Stand; superseded durch #21 body @ afc86a8f und 5354522420
Begründung: Die alten Auditbefunde zeigten konkurrierende Autoren für Executor-Auflösung und Registry. Der aktuelle Owner weist den deploybaren Atelier Runner, seine Agent Executor Adapters, Manifestidentität und Containment #301 zu. Dieses Requirement erhält den historischen Anlass, aber weder den alten Ist-Befund noch einen zweiten Umsetzungsplan.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-23: Das Tor zum Bau der Ferne ist Wert, kein Kalender.
Status:     DRAFT
Quelle:     DESK — 5302602114 §1 (alte Regel 23); geschärft durch #21 body @ afc86a8f
Begründung: Allgemeine Remote-Ausführung bleibt an einen benannten Bedarf gebunden. Der aktuelle Owner benennt den schmalen Agent-only-CI-Carrier-Proof als diesen ersten Wert und lässt seine Lane nach `#15-B` beginnen; sie blockiert den lokalen Cutover nicht. Weitere Carrier werden erst an einem eigenen belegten Bedarf gebaut, nicht vorsorglich.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-24: Verwaltete Provider-Sandkästen sind ein Beobachtungspunkt, kein Wettbewerber, gegen den gebaut wird.
Status:     DRAFT
Quelle:     DESK — 5302602114 §3 (Regel 24)
Begründung: Reift die Ausführung in der Cloud eines Providers unter Operator-Richtlinien, kann ein Agent Executor Adapter diesen Vollzug konsumieren; die Platzierungsnaht — attestierte Fähigkeit — und der bounded Worker-Vertrag bleiben erhalten. Selbst gehostet bleibt der Souveränitätspfad. Die Anweisung lautet, zu beobachten und die Naht zu halten, nicht eine der beiden Seiten vorzubauen.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-25: Langlebige Runner werden einzeln eingeschrieben; ephemere CI-Jobs werden nur durch eine enge TrustPolicy zugelassen.
Status:     DRAFT
Quelle:     DESK — #21 body @ afc86a8f; 5354522420
Begründung: Ein langlebiger Runner braucht eine eigene widerrufbare Identität und Attestation. Ein ephemerer CI-Job wird nicht manuell als Runner eingeschrieben: eine gepinnte OIDC-Issuer-, Repository/Projekt-, Workflow/Config- und Ref/Environment-Policy tauscht genau eine eindeutige Job-Assertion gegen ein kurzlebiges Credential für einen Attempt, eine Generation und eine Worker-Rolle. Replay oder ein abweichender Claim verweigert.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-26: Agent Runner und Effect Worker teilen weder Vertrag noch Identität, Credential, Environment oder Privilegpfad.
Status:     DRAFT
Quelle:     DESK — #21 body @ afc86a8f; 5354522420; 5354786342
Begründung: Der Atelier Runner führt genau einen `AgentAttempt` über einen Agent Executor Adapter aus. Ein Effect Worker führt genau einen vorbereiteten `EffectIntent` über den bestehenden Effect Adapter unter einem operation-scoped Grant aus. Core allein schreibt Agent- oder Effect-Receipt und behält Wait, Join, Resume und Scheduling.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-27: Serve und Runner sind getrennte OCI-Images und Release-Artefakte, und Serve erhält weder Provider-Secret noch rohe Carrier-Autorität.
Status:     DRAFT
Quelle:     DESK — 5354196886; #21 body @ afc86a8f; 5354522420
Begründung: Core/Serve besitzt dauerhafte Wahrheit; der Runner besitzt Provider-CLI, lokale Credential-Auflösung, Attempt-Vollzug und terminale Evidenz. Provider-CLI, Provider-Credential-Werte, Docker/OCI-Socket, systemd/DBus und privilegierte Broker überschreiten die Serve-Grenze nicht. Packaging und Cutover bleiben auf #312 statt hier dupliziert.
Journeys:
Beweis:     UNGEBUNDEN
Offen:

### REQ-REMOTE-28: Carrier-gebundener Bau bleibt verweigert, bis der Operator Carrier, Launch-Autorität und Mutual Auth entschieden hat.
Status:     DRAFT
Quelle:     DESK — #21 body @ afc86a8f; 5354779824; supersedes REQ-REMOTE-03
Begründung: Der Owner entscheidet die Invarianten, aber er erfindet den konkreten Mechanismus nicht. Ein Disposable-Host-Proof geht der Entscheidung voraus; bis dahin bleiben carrier-gebundenes `#301-A`, CI-Ausführung und Remote-Verfügbarkeit gestoppt.
Journeys:
Beweis:     UNGEBUNDEN
Offen:      - Welcher Carrier, welche Launch-/Cleanup-Instanz und welcher Mutual-Auth-Mechanismus werden gewählt? (Eigentümer: Operator via #21, Ziel: Ruling nach Disposable-Host-Proof)

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

- **Carrier, launch authority and concrete mutual authentication remain open.**
  #21 owns the operator ruling after a disposable-host proof. #15 owns the
  lease/fencing/evidence-acknowledgement contract, #301 the Agent Runner, and
  #312 separate artifacts and cutover. REQ-REMOTE-18 points to their canonical
  sequence without copying their acceptance. REQ-REMOTE-11 and -28 keep remote
  and CI execution refused until the gate closes.

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

## Acceptance

No story has declared an acceptance sentence for this requirement, so what
follows is a set of candidates and not a set of declared sentences; none of
them has an identifier to name. The rules above already state in testable form, notably:

- a long-lived process without enrolment and an ephemeral CI job outside its
  TrustPolicy receive no credential or attempt;
- a binding that no connected, authorized Runner attests starts nothing — no
  durable run, no attempt, no provider process;
- a runner whose bound credential reference does not resolve on its host refuses
  rather than falling back to another auth mode;
- a running attempt never changes machine, and a lost runner yields
  `POSSIBLY_RAN` rather than a second placement;
- before `LAUNCH_ARMED`, reassignment requires authoritative no-launch evidence;
- a resumed or replaced attempt appears as a new receipted attempt, on a runner
  chosen by matching and not by a typed label;
- a runner attestation that differs from the enrolled one is a visible diff
  requiring a fresh operator act, never a silent widening;
- no provider credential value appears in any record, projection, or
  transmission in either direction;
- a short-lived worker-identity credential is issued only after its one-time
  TrustPolicy exchange, is scoped to one attempt, generation and role, and its
  value appears in no durable Atelier state, log or carrier artifact;
- Agent Runner and Effect Worker credentials, environments and privilege lanes
  are mutually unusable;
- the Serve artifact contains no provider CLI, provider credential or raw
  carrier authority, and CI artifact expiry removes no Core truth;
- attach is off unless separately enabled, and every attach is audited.

The environment-requirements gap above is deliberately absent from this list:
until that vocabulary exists there is no honest sentence to write for it, and an
acceptance sentence invented ahead of its requirement is the failure this
directory's convention exists to prevent.
