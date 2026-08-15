# Requirement 0003: One workshop, three views, one language — the graph

```text
Status:         AGREED
Owner-Issue:    https://github.com/FlexOr2/atelier-2/issues/9
Source-Threads: #9
Distilled-From: 5294009202, 5301898411, 5302066517, 5302109868, 5302132001,
                5302769095, 5302788411
```

`AGREED` covers the operator decisions and the applied amendment below. Where
the thread still labels something a proposal — chiefly the order in which the
three parts are absorbed — it appears under `Open questions`, not as a rule.

## Intent

The operator sees every run as a living graph — event-sourced, streamed — with
attempts optionally as **ephemeral tiles** that spawn and end with the attempt,
never as permanent seats. Intervening uses the *native* interactivity of the
provider console; none of it is rebuilt. (#9 body, 5294009202.)

Around that core sits the target UI: one workshop, three views, one language —
the graph. (5301898411.)

The operator's standard for it, verbatim (5301898411):

> „simple — ich kann alles machen was ich will — alles einstellen wenn ich will.
> Intuitiv und es muss Spaß machen und cool aussehen. Und natürlich alles
> funktionieren."

And the admission criterion for anything added to it: it must **remove work**,
not add features. (5302788411.)

## Binding rules

1. **The project is the primary structure, not a topbar switch.** Three levels
   (5302769095, from the operator at mockup v3):
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

2. **Every screen answers exactly one question.** Studio: what is happening?
   Project: what is happening here? Run: what is it doing? Library: what do I
   have? Settings: what applies? Two questions ⇒ split the screen. (5302788411.)

3. **Two doors lead to the same canvas.** Either the chat with the conductor
   (#7) composes a graph and shows it *before* the start, or the construction
   kit lets agents, skills, and subworkflows be dragged from the library onto
   the canvas, connected, and configured by click — model, tools, budget,
   prompt: everything settable, nothing that must be set. Both doors produce the
   same object, the V3 graph. The conductor pre-fills; the human adjusts.
   (5301898411 §2.)

4. **Project choice comes first**; what the fleet is working on is first class.
   (5301898411 §1.)

5. **The library shows names, never hashes.** Named, described, versioned agents
   in the markdown format of #66, skills, and workflows with their scorecard
   (#8). A hash is never again a selection option; #22 owns the names.
   (5301898411 §3.)

6. **The same graph is the live view.** Nodes light up working / completed /
   failed; a click on a running agent opens its ephemeral tile with live output
   and native intervention. A live run in the runs list opens that same graph in
   its live state. (5301898411 §4, 5302066517 §2.)

7. **Mode is a capability declaration.** Headless is mandatory for every
   provider; interactive is declared. A node that demands interactive fails at
   *validation* on a provider that does not declare it — never silently. The
   enum field belongs in the B0.1 binding, before the executor contract freezes.
   (#9 body, marked as a binding operator decision.)

8. **Interactive attach is V1 local-only, and it marks the run.** The operator
   chooses the mode per node or run. Interactive attempts run in the same
   isolated workspace with the same declared capability set; where that equality
   cannot be enforced, interactive is UNAVAILABLE for the node — fail-closed. An
   interactive node either declares no typed output (legal only without
   downstream mapping) or ends with an explicitly operator-confirmed artefact.
   The run and its downstream outputs count as *operator-influenced* and are
   excluded from the #8 balance. The boundary is a state-changing action between
   start and terminal: **pure observation never touches** — a hover preview or
   an opened tile without input does not mark the run. (#9 body part 2,
   5302132001 §2 closing.)

9. **The live tile reads the ephemeral runner channel; raw provider frames never
   enter an event or a receipt.** (5302132001 §2, which sharpens 5302066517 §3
   and part 2 of the item body, because Issue #1's invariant is absolute: raw
   provider frames never appear in event, log, receipt, database, crash
   evidence, or workspace.)
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

10. **Completed runs get history replay, not byte-exact replay.** The button
    replays the durable chain of proof: node transitions, bound revisions and
    hashes, declared outputs, terminal receipts — and it enumerates next to
    itself what is replayable. What is not replayable, the UI does not claim.
    Not durable today: intermediate steps, tool calls, stderr, and any timeline
    at all. (5302132001 §1, replacing the "byte-genau aus den Receipts
    nachspielbar" wording of 5302066517 §2.)

11. **V1 builds project memory; per-user memory is gated on #82.** Own agents,
    favourites, and defaults hang on the project, which #23 and #79 make first
    class anyway, and that is definable without a notion of identity. Per-user
    memory is a named successor with an explicit dependency on requirement 0002
    (#82) — before it there is no user for anything to hang on. The same holds
    for the "Users / Audit" settings area: it is #82-gated, not "later".
    (5302132001 §3, replacing "Per-User/Projekt-Gedächtnis (später)" of
    5301898411 §5.)

12. **Language and naming:** English by default, German optional; short names —
    "Studio", not "Leinwand & Chat". (5302066517 §1.)

13. **The runs list shows purpose and result, never only status.** A project
    chip on every row plus a project filter, and consumption or cost per row
    (#8). (5302066517 §4.)

14. **One exchangeable design-token system**, light / dark / auto everywhere,
    and notifications as an inbox — cards, when a run needs a human.
    (5302066517 §5; the ⌘K command palette from the same comment is deferred
    until there is enough to command, per 5302132001.)

15. **Settings are a professional surface with no hardcoded provider rows.**
    "Providers" is a list of connected providers ("+ Add provider" → provider →
    login *or* token, the method being the operator's choice); then Projects
    (repository + tracker + credential reference + rules, pause ↔ resume), Agent
    defaults (model per role class — gates and judgments opus, samples haiku;
    budget frames that are never in the way and never off), Runners,
    Notifications, Appearance / Language. (5302066517 §6.)

16. **Four ingredients that remove work** (5302788411):
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

17. **Auth adds complexity to no screen.** Login is the page before; identity is
    the avatar menu; roles change what *renders* — a viewer sees no start button
    rather than collecting refusals. Because of this rule, authentication
    (requirement 0002, #82) may arrive later without contorting the design.
    (5302788411.)

18. **Mockups are design templates, explicitly.** The built UI may look better
    or different; what binds is the information hierarchy of rule 1 and that
    every display shows receipted truth. (5302769095. The referenced artefact is
    mockup v3, held by the operator, clickable, at the same URL as v1 and v2 —
    it is not in this repository. 5302066517 closing.)

19. **Atelier 1 is reused as concepts and lessons, never as ported code.** The
    concrete lessons are kept as a prior-art note — ttyd version and sha
    pinning, Codex per-directory trust, tmux ≥ 3.4 in containers because of the
    argv0 output, capture-pane diagnosis — so that "reuse" never smuggles the
    permanent-seat machinery back in. (#9 body.)

## Open questions

- **Remote attach is its own epic**, gated on a runner-trust ADR: runner
  identity and registration, operator auth, per-attach step-up, short-lived
  terminal tokens, attach audit. Location transparency stays the *goal*, as a
  designed seam — nothing bakes in localhost — but not as a V1 build. ADR 0001's
  supervision contract is explicitly local-runner scope; remote runners need
  their own attempt-ownership contract (lease, heartbeat, fencing) that
  preserves exactly-once and cleanup. (#9 body part 3.)
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
  never writes a source"). The ruling for it is the kind `unsourced` with export
  as the way *to* a source, never as writing *into* the source; it goes into the
  next revision of PR #45 (see #22). (5302132001 closing.)
- **Deferred, as named successors:** the ⌘K command palette and the multi-run
  tile wall — weight without carrying power while there is one kind of run and a
  handful of objects. Bringing them back later costs nothing. (5302132001.)
- **The absorption order of the three parts is a plan, not a rule**: graph view
  with ephemeral headless tiles first, then local interactive attach, then
  remote attach as its own epic. The substrate distance is named honestly — ADR
  0002 is a single-successor chain today, and the graph needs fan-out and fan-in
  first. (#9 body.)

## Acceptance

No story has declared an acceptance sentence for this requirement yet. The
sentences a story would have to declare are already stated in testable form by
the rules above — notably: a node demanding an undeclared interactive mode is
refused at validation; an opened tile without input leaves the run out of the
operator-influenced set; no raw provider frame is readable from any event or
receipt after a live tile was watched; the replay button enumerates exactly what
it replays; a viewer role renders no start button.
