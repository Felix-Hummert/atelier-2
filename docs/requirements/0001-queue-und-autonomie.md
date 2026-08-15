# Requirement 0001: Items are prioritised, get their workflow, and run from a queue

```text
Status:         AGREED
Owner-Issue:    https://github.com/FlexOr2/atelier-2/issues/79
Source-Threads: #79
Distilled-From: 5302017656, 5302022156, 5302048197, 5302062963, 5302109936,
                5302131944, 5302732436
```

## Intent

A project is connected to a tracker — GitHub, GitLab, whichever is configured.
Work items arise there, written by humans or by workflows. From then on it runs
by itself: items are prioritised, get the workflow that fits them, land in a
queue, and start as soon as their preconditions are met — visibly, live. Not
everything is worked automatically; a filter decides what agents may take and
what belongs to a human. (#79 body.)

This is the mechanisation of the way the fleet already works by hand: issue →
triage → queue → precondition-driven start → observable processing. The
conductor in continuous operation instead of once per chat turn. (#79 body.)

The north star the operator stated for this item and its siblings, verbatim
(5302732436):

> „Ich sage meine Vision — und möchte, dass danach mit Workflows alles autonom
> abgearbeitet wird, bis das Produkt fertig ist. Außer ich schreite irgendwo
> ein."

The counter-image is named: today the operator has to nudge the coordinator and
ask whether useful work is still happening. That is the illness the queue cures.

The composition is fully decided across its owners: vision → vision planning
(#6) → breakdown planner (#80) → item tree with dependencies → the queue (#79)
pulls every ready piece through its workflow until the tree is empty. **Done =
every piece of the epic landed** — the machine-readable definition of "the
product is finished". (5302732436.)

## Binding rules

1. **Triage proposes, it does not decide.** Priority is business judgment: the
   default is a proposal (priority + workflow) with one-click confirmation.
   Auto-confirmation is unlockable per item class once the scorecard shows it
   earned. Full automation is earned, never assumed. (5302017656 §1.)

2. **Assignment comes from evidence.** Workflow assignment is fed by the #8
   measurements — workflow × item-class performance. Cold start with
   hand-written rules, then learning. #79 and #8 are coupled. (5302017656 §2.)

3. **The queue cap is denominated in what the atelier can count without the
   provider.** (a) started attempts per time window and (b) summed
   `attempt_deadline_seconds` admission; optionally (c) a token cap **per meter
   revision**, never a sum across meter revisions. The parallelism cap stays a
   count of concurrent attempts. All three are visible and **refuse instead of
   truncating**. (5302131944 §1, which replaces the undenominated "Tages-Deckel"
   of 5302017656 §3 — a daily token cap over a queue spanning several models and
   providers is exactly the cross-meter sum ADR 0008 forbids.)

   The same counter is today's only honest consumption alarm: it is what can
   tell the operator *beforehand* that it is getting tight.

4. **Red does not circle.** Bounded retry, then escalation to the inbox.
   (5302017656 §4.)

5. **The queue automates the starting, never the releasing.** This sentence
   belongs in the acceptance verbatim, because it is what separates this feature
   from an accident (5302017656 §4, kept literal by 5302109936 and 5302131944):

   > „Die Queue automatisiert das ANSTOSSEN, nie das FREIGEBEN — Verdict-Tore in
   > den Workflows bleiben, Durchsatz schlägt nie Review."

6. **Deep links in both directions, tracker-neutral.** Every run and workflow in
   the atelier UI links its work item; the queue start writes the back link at
   the item — to the live graph, finally to the receipted result. Owner cut: the
   link semantics (what is written when, idempotent, readback-verified) belong
   to the #24 adapter contract, the UI side (item chip on the run row and on the
   graph) to #9. (5302022156.)

7. **Pause means drain; resume means releasing the ready set.** Pause starts no
   new node and lets running attempts drain to their terminal receipt; resume
   releases the ready set again. "Lossless" refers to the state of the *queue* —
   item order, assignments, satisfied preconditions — never to an in-flight
   attempt. (5302131944 §2, which replaces the park-and-save wording of
   5302048197: a running provider attempt cannot save itself hand-over-ready.
   Native provider session resume promises neither stream replay nor
   exactly-once model calls, the adapter runs `--no-session-persistence`, and
   cancel ends in exactly one terminal cancel receipt — a paused attempt would
   be a cancelled attempt whose restart is a new paid call without the half-done
   work.)

8. **A label authorises work only when the actor that set it is not the atelier
   itself.** In PAT mode labels are an *observed* set: the #24 operations
   registry contains **no write operation** for the authorising label, and the
   actor check runs on the absence of the atelier's content marker — the marker
   is a hint, the missing write operation is the control. In app mode the check
   is trivial by account identity. (5302131944 §3, sharpening the canonical
   scenario of 5302062963: under the PAT ruling an agent could otherwise
   authorise its own work and spending by writing a label; no attacker is
   needed, a bug or a misread prompt suffices, and prompts are not a control.)

9. **A project is a configured bundle:** repository URL + tracker + credential
   *reference* (never plaintext, ADR 0009) + workflow assignment rules + item
   filter. Owner cut: project isolation is decided by #23, the adapter by #24,
   the queue rules here. (5302062963 §1.)

10. **Working means a clone per attempt, never the operator's checkout**; the
    result comes back as a branch or pull request. This makes the workspace term
    precise for #60: the attempt-unique working directory is seeded from the
    project clone, while scratch (#58) stays the provider's write surface beside
    it. (5302062963 §2.)

11. **A CI gate is a workflow detail, not part of the platform connection.**
    (5302062963 §3.)

12. **Processing is sequential or parallel up to the queue cap** — visible,
    receipted, with pause ↔ resume. (5302062963 §4 with 5302048197.)

13. **Human gates are four precise places set by the operator, never one global
    switch** (5302732436):
    1. the **workflow gate** — a Wait node: "show me X before you do Y";
    2. the **queue-rule gate** — item classes autonomous versus
       confirmation-required, the human filter of this item, with the stage
       mechanics of #106: granted by the operator, revocable by measurement;
    3. the **budget gate** — the caps of rule 3: run hot ⇒ visible stop, never
       silent further burning;
    4. the **inbox gate** (#9) — everything that needs the human (question,
       gate, red after bounded retries) arrives as a card *at* him, with pause ↔
       resume per project (#23) as the emergency stop and restart.

## Open questions

- **Retention has no owner yet, and it is due before a second project.** Foreign
  work code and foreign issue text land in a store from which they cannot be
  removed: every durable table carries a `no_delete` trigger, and there is no
  pruning, no TTL, and no run or project deletion — the only way out is
  discarding the whole SQLite file. The decision now sits with **#23**, together
  with isolation, and is due *before* a second — especially a foreign — project
  is connected. (5302109936 §4b, assigned in 5302131944 §4b.)
- **Reachability is a named precondition, not a given.** ADR 0009 refuses every
  non-loopback bind without an operator authenticator, and today's situation is
  sharper than that draft: `serve --host 0.0.0.0` without a composed Claude
  executor binds publicly and entirely unauthenticated. (5302109936 §4a,
  5302131944 §4a.)
- **Whether a personal subscription may be spent on someone else's work code is
  not a technical question** and no technical item answers it. (5302109936 §4c.)
- **The stated order is much shorter than the real chain.** This item sits
  behind #15 → #58 → #60 → #38 → the V3 chain → #16 phase 2 + #63 → the #24
  implementation → #23: eight owners, two of them not schedulable. The vision is
  not wrong; the sentence "Reihenfolge: nach Funktions-Kette (#60/#38) und #24"
  is just far shorter than the chain it names. (5302109936 §5.)

## Acceptance

The canonical acceptance scenario is a literal operator sentence — the thread
states that this sentence *is* the acceptance test of this item (5302062963):

> „Ich binde extern GitHub (oder GitLab — Beispiel) mit einem Token an,
> referenziere das Projekt. Ich setze den Filter: *bearbeite alle Issues, die
> `backlog` als Label im Projekt haben, und nutze den XXX-Workflow.* Dann Start
> — und es arbeitet alles nacheinander weg. Ich kann das Atelier so auch von
> meiner Arbeit aus nutzen, zum Test."

The acceptance direction, to be refined into declared sentences before the build
(#79 body), plus the two sentences the thread ruled literal:

- A newly created item appears in the queue already triaged: priority, assigned
  workflow, and ready state with its open preconditions named.
- A filter or rule set decides which items may run automatically; everything
  else waits visibly for a human.
- Becoming ready starts the bound workflow with no further handling, and the run
  is live on the graph (#9).
- Everything is receipted; no start outside the budget and authorisation rules.
- Verbatim, from rule 5: „Die Queue automatisiert das ANSTOSSEN, nie das
  FREIGEBEN — Verdict-Tore in den Workflows bleiben, Durchsatz schlägt nie
  Review."
- The canonical scenario above, carrying the two named preconditions from
  `Open questions` (reachability, retention).

No story has declared any of these as an acceptance sentence yet, so none of
them carries a sentence identifier or a proof.
