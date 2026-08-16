# Requirement 0003: One workshop, three views, one language — the graph

```text
Status:         AGREED
Owner-Issue:    https://github.com/FlexOr2/atelier-2/issues/9
Source-Threads: #9, #5
Distilled-From: 5294009202, 5294316639, 5301898411, 5302066517, 5302109868,
                5302132001, 5302769095, 5302788411, 5307632332
                #9 body, sha256
                36800d6ecd5d3e8922028425835b368b42d163098e5d32da930e40d25f49ce99
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

The thread quotes the operator in several places, and exactly one of those
quotations states a rule. **Exactly one rule below is graded `OPERATOR`** —
rule 1, on the sentence 5302769095 quotes from him. The other quotations are his
standard, reproduced under `Intent`; his nine words of dissatisfaction with the
mockup's Settings screen, reproduced at rule 15; and the questions he asked,
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

## Rules

1. `OPERATOR` — **The project is the primary structure, not a topbar switch.**
   Three levels (5302769095, quoting the operator at mockup v3: „das Studio hat
   den Chat, es sollte die aktiven Projekte zeigen; das Projekt hat die
   laufenden Workflows". The card and inbox detail below it is desk detail):
   1. **Studio (home)** — chat with the conductor, global and context-aware,
      plus the active projects as *living cards* ("`<project>` · n running · n
      waiting for you · last landing X ago"), plus an inbox row across the top,
      cross-project, carrying whatever needs the human.
   2. **Project** — the queue (ready / running / waiting), the running workflows
      as *mini graphs* (the "this is what is being done right now" glance),
      rules and sources, pause ↔ resume.
   3. **Run** — the full canvas with nodes, tiles, and intervention.

   The chat is the same door at every level, carrying that level's context:
   Studio = everything, Project = this one, Run = this one.

2. `DESK` — **Every screen answers exactly one question.** Studio: what is
   happening? Project: what is happening here? Run: what is it doing? Library:
   what do I have? Settings: what applies? Two questions ⇒ split the screen.
   (5302788411, the desk's curation in answer to the operator's question „was
   noch einbauen, ohne zu überladen?".)

3. `DESK` — **Two doors lead to the same canvas.** Either the chat with the
   conductor (#7) composes a graph and shows it *before* the start, or the
   construction kit lets agents, skills, and subworkflows be dragged from the
   library onto the canvas, connected, and configured by click — model, tools,
   budget, prompt: everything settable, nothing that must be set. Both doors
   produce the same object, the V3 graph. The conductor pre-fills; the human
   adjusts. (5301898411 §2.)

4. `DESK` — **Project choice comes first**; what the fleet is working on is
   first class. (5301898411 §1.)

5. `DESK` — **The library shows names, never hashes.** Named, described,
   versioned agents in the markdown format of #66, skills, and workflows with
   their scorecard (#8). A hash is never again a selection option; #22 owns the
   names. (5301898411 §3.)

6. `DESK` — **The same graph is the live view.** Nodes light up working /
   completed / failed; a click on a running agent opens its ephemeral tile with
   live output and native intervention. A live run in the runs list opens that
   same graph in its live state. (5301898411 §4, 5302066517 §2.)

   Rules 3 to 6 read the numbered sections of 5301898411, a comment headed
   "OPERATOR-VISION VERSCHÄRFT (Felix …)". The one sentence it quotes from him
   is the standard reproduced under `Intent`; §1 to §5 are the desk's prose, so
   these four rules are `DESK` however faithfully they render the vision.

7. `DESK` — **Mode is a capability declaration.** Headless is mandatory for
   every provider; interactive is declared. A node that demands interactive
   fails at *validation* on a provider that does not declare it — never
   silently. The enum field belongs in the B0.1 binding, before the executor
   contract freezes. (#9 body @ 36800d6e, which marks this paragraph
   "OPERATOR-ENTSCHEIDUNG (bindend)", and comment 5294316639 on #5, which
   records the same decision in the same words.)

   The grade is `DESK` because both objects are desk prose *labelling* the
   decision as the operator's; neither quotes him. That is a statement about
   what this thread evidences, not a licence to reopen the decision: it stands
   where it was made, the B0.1 binding follows it, and whoever needs it settled
   asks the operator rather than reading a certification out of this file.

8. `DESK` — **Interactive attach is V1 local-only, and it marks the run.** The
   operator chooses the mode per node or run. Interactive attempts run in the
   same isolated workspace with the same declared capability set; where that
   equality cannot be enforced, interactive is UNAVAILABLE for the node —
   fail-closed. An interactive node either declares no typed output (legal only
   without downstream mapping) or ends with an explicitly operator-confirmed
   artefact. The run and its downstream outputs count as *operator-influenced*
   and are excluded from the #8 balance. The boundary is a state-changing
   action between start and terminal: **pure observation never touches** — a
   hover preview or an opened tile without input does not mark the run. (#9
   body @ 36800d6e part 2, 5302132001 §2 closing — both desk text; the body
   labels only rule 7 as an operator decision.)

9. `DESK` — **The live tile reads the ephemeral runner channel; raw provider
   frames never enter an event or a receipt.** (5302132001 §2, which sharpens
   5302066517 §3 and part 2 of the item body, because Issue #1's invariant is
   absolute: raw provider frames never appear in event, log, receipt, database,
   crash evidence, or workspace.)
   - **Live view = ephemeral runner channel.** The tile and the hover preview
     read the runner's ephemeral channel — the same path ADR 0009 decides for
     attach, with a per-attach ticket and attach audit. What flows there is a
     redacted, non-durable projection: no event, no receipt, stored nowhere. A
     watcher sees the work; the database never does.
   - **Durable is hash plus bounded output.** The receipt carries hash and
     location, never the transcript bytes. An archive copy — if one ever exists
     — lies outside the receipt and under the retention decision, which now sits
     with #23. While that decision is missing, there is no archive copy.

   The consequence for the roadmap is explicit: a live view is architecture — a
   streaming mode, a runner channel, redaction — not a UI line.

10. `DESK` — **Completed runs get history replay, not byte-exact replay.** The
    button replays the durable chain of proof: node transitions, bound
    revisions and hashes, declared outputs, terminal receipts — and it
    enumerates next to itself what is replayable. What is not replayable, the
    UI does not claim. Not durable today: intermediate steps, tool calls,
    stderr, and any timeline at all. (5302132001 §1, replacing the "byte-genau
    aus den Receipts nachspielbar" wording of 5302066517 §2.)

    **This narrows a promise made in a comment headed as the operator's.**
    5302066517 is headed "Operator-Feedback-Runde 2" and its §2 promises
    byte-exact replay, but quotes no operator sentence, so the promise is desk
    prose and this rule corrects one desk reading with another. It is flagged
    all the same, because the promise may render something he said at the
    mockup: the desk's reading is that nothing durable today could produce it,
    and that reading is a proposal to him, not a settled correction.

11. `DESK` — **V1 builds project memory; per-user memory is gated on #82.** Own
    agents, favourites, and defaults hang on the project, which #23 and #79
    make first class anyway, and that is definable without a notion of
    identity. Per-user memory is a named successor with an explicit dependency
    on requirement 0002 (#82) — before it there is no user for anything to hang
    on. The same holds for the "Users / Audit" settings area: it is #82-gated,
    not "later". (5302132001 §3, replacing "Per-User/Projekt-Gedächtnis
    (später)" of 5301898411 §5.)

12. `DESK` — **Language and naming:** English by default, German optional;
    short names — "Studio", not "Leinwand & Chat". (5302066517 §1.)

13. `DESK` — **The runs list shows purpose and result, never only status.**
    A project chip on every row plus a project filter, and consumption or cost
    per row (#8). (5302066517 §4.)

14. `DESK` — **One exchangeable design-token system**, light / dark / auto
    everywhere, and notifications as an inbox — cards, when a run needs a
    human. (5302066517 §5; the ⌘K command palette from the same comment is
    deferred until there is enough to command, per 5302132001.)

15. `DESK` — **Settings are a professional surface with no hardcoded provider
    rows.** The operator content behind this rule is one sentence, quoted at
    5302066517 §6:

    > „wie sie aufgebaut ist mag ich noch nicht"

    Nine words about the Settings screen of the mockup, and they say only what
    he does not want. Everything the rule proposes in their place is the desk's
    answer and binds nothing until he rules it: "Providers" as a list of
    connected providers ("+ Add provider" → provider → login *or* token, the
    method being the operator's choice); then Projects (repository + tracker +
    credential reference + rules, pause ↔ resume), Agent defaults (model per
    role class — gates and judgments opus, samples haiku; budget frames that are
    never in the way and never off), Runners, Notifications, Appearance /
    Language.

    Rules 12 to 15 all come from 5302066517, whose header names an operator
    feedback round; §1, §4 and §5 quote him nowhere, and §6 quotes only the
    rejection above.

16. `DESK` — **Four ingredients that remove work** (5302788411, the desk's
    curation; the admission criterion "removes work" is the desk's too):
    1. **Workshop pulse** — one always-visible header line: quota consumed
       today, runner health, queue depth. Not a dashboard; it abolishes the
       atelier-1 surprise of hitting the 100 % limit by design.
    2. **The receipt as a jewel** — every ✓ opens a beautiful human-readable
       receipt page (what ran, what it saw, what it checked, the hash chain as a
       graphic). The differentiator deserves jeweller's treatment; it is the
       later home of the dossier export (#104).
    3. **Teaching empty states** — every empty screen shows *the one* next
       action. Onboarding without a tutorial.
    4. **Undo instead of asking** — a five-second undo toast instead of
       confirmation dialogs.

    Deliberately excluded: dashboards beside the pulse, a notification centre
    beside the inbox, charts on home.

17. `DESK` — **Auth adds complexity to no screen.** Login is the page before;
    identity is the avatar menu; roles change what *renders* — a viewer sees no
    start button rather than collecting refusals. Because of this rule,
    authentication (requirement 0002, #82) may arrive later without contorting
    the design. (5302788411.)

18. `DESK` — **Mockups are design templates.** The built UI may look better or
    different; what binds is the information hierarchy of rule 1 and that every
    display shows receipted truth. (5302769095 writes this as "Mockups sind
    Design-VORLAGEN (Operator ausdrücklich)" — a parenthetical attribution, not
    a quoted sentence, so the rule is `DESK` even though the same comment quotes
    him for rule 1. The referenced artefact is mockup v3, held by the operator,
    clickable, at the same URL as v1 and v2 — it is not in this repository.
    5302066517 closing.)

19. `DESK` — **Atelier 1 is reused as concepts and lessons, never as ported
    code.** The concrete lessons are kept as a prior-art note — ttyd version
    and sha pinning, Codex per-directory trust, tmux ≥ 3.4 in containers
    because of the argv0 output, capture-pane diagnosis — so that "reuse" never
    smuggles the permanent-seat machinery back in. (#9 body @ 36800d6e.)

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

No story has declared an acceptance sentence for this requirement yet, so the
list below is a set of candidates and not a set of sentences. What the rules above already state in
testable form is, notably: a node demanding an undeclared interactive mode is
refused at validation; an opened tile without input leaves the run out of the
operator-influenced set; no raw provider frame is readable from any event or
receipt after a live tile was watched; the replay button enumerates exactly what
it replays; a viewer role renders no start button.
