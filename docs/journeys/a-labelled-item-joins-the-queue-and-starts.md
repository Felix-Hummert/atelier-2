# A labelled item joins the queue and starts

This is REQ 0001's own acceptance test, the operator's canonical scenario,
recorded as *wörtlich sinngemäß* — a rendering close to his words, not a
transcript (operator, 16.08.2026, 5302062963):

> „Ich binde extern GitHub (oder GitLab — Beispiel) mit einem Token an,
> referenziere das Projekt. Ich setze den Filter: *bearbeite alle Issues, die
> `backlog` als Label im Projekt haben, und nutze den XXX-Workflow.* Dann Start
> — und es arbeitet alles nacheinander weg. Ich kann das Atelier so auch von
> meiner Arbeit aus nutzen, zum Test."

What the running system does with that walk today, and where #79 still names
a gap:

1. **Bind and set the filter.** A project names its tracker and, in its queue
   policy, one automation label
   (`PUT /projects/{public_project_reference}/queue-policy`). The
   tracker stays the one source of item truth; the Atelier holds no second
   copy of item state (REQ-QUEUE-09, REQ-QUEUE-14). No label named means no
   automatic admission at all.
2. **Triage decides throughput, not the verdict.** The sweep reads the tracker
   at decision time and admits every item carrying a proposal whose label
   matches the policy, under the `AUTOMATION_RULE` authority
   (`the-automation-label-admits-the-items-that-carry-it`); every other item
   still waits for a human's one-click confirmation (REQ-QUEUE-01,
   REQ-QUEUE-08). That proposal — priority and workflow — is either one a
   person wrote and inspected through `PUT /queue-proposals`, or one the sweep
   wrote from the policy's own `default_workflow_lineage_id` and
   `default_priority_rank`, which no one inspected per item: there the
   inspection happened once, at the policy. When that policy also states
   `automation_disposition_default: AUTOMATION_AUTHORIZED`, setting the label
   is the whole human act that launches an agent on the item and spends
   provider money on it. The label authorises only because a human sets it in
   the tracker: the platform adapter carries no write path for that label, so
   the Atelier itself can never grant its own admission.
3. **Start, once.** Admission and start run in the same sweep. An admitted
   item starts bound to one exact run and workflow revision; a repeated sweep
   or a moved lineage head reports the existing run rather than a second one
   (`a-manually-approved-queue-item-starts-once`). The queue automates only
   this launch, never a workflow's own verdict gates — REQ-QUEUE-05 holds
   unchanged through that automation.
4. **Keep the rest moving.** A start the sweep refuses stays visible at its
   own item; the sweep's other admitted items still start in the same pass
   (`a-refused-queue-start-stays-at-its-item-while-the-sweep-continues`),
   which is REQ-QUEUE-04's promise that red does not spin the whole queue in
   a circle. The live project's queue policy holds the cap the sweep starts
   against (REQ-QUEUE-03, REQ-QUEUE-12): work above it waits in priority
   order instead of starting anyway.

What is not landed yet: nothing *derives* a workflow or a priority from an
item's own content — "und nutze den XXX-Workflow" is the one workflow the
policy names for every labelled item, not a per-item measurement (REQ-QUEUE-02,
#79). No surface shows the queue as
its own view — Studio's start sheet calls the same read door only to fill its
picker (#79, Scheibe B) — so REQ-QUEUE-13's Queue-Regel-Tor and REQ-QUEUE-15's
bound, visible priority have no place to show themselves yet. And the walk
above has not run end to end on a real issue with real provider money (#79,
Scheibe D). The two preconditions the thread once bound to this scenario —
network reachability and workspace retention — are open questions on #79, not
requirement text.

Illustriert: REQ-QUEUE-01, REQ-QUEUE-03, REQ-QUEUE-04, REQ-QUEUE-05,
REQ-QUEUE-08, REQ-QUEUE-09, REQ-QUEUE-12, REQ-QUEUE-14
