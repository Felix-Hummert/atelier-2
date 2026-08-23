<script lang="ts">
  import type { AnyRun, CockpitApi, RunV3 } from "../api/client";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { humanErrorMessage } from "../lib/humanRefusal";
  import { MutationJournal, waitAnswerText, type WaitMutation } from "../lib/mutationJournal";
  import { runPageCopy } from "../lib/runPageCopy";
  import { runPath } from "../lib/route";
  import { studioPageCopy } from "../lib/studioPageCopy";
  import { studioQuestions } from "../lib/studioQuestions";
  import {
    deliverWaitAnswer,
    loadPendingWaitAnswer,
    prepareWaitAnswer
  } from "../lib/waitAnswerDelivery";
  import { confirmedDecisionLabel, decisionLabel } from "../lib/waitDecision";

  /**
   * The inline decision a boolean/enum wait gate answers on its own Board
   * card (#572): the same audited POST path the run page's composer uses
   * (`waitAnswerDelivery.ts`), so a decision made here carries the identical
   * durable pending/uncertain/retry journal, never a second implementation.
   *
   * The workflow's published graph names a wait's `kind` -- the one fact
   * that decides whether this card can offer buttons at all. Reading it costs
   * one network call, so this component defers that call until the operator
   * actually opens the card (`toggle`), never for every waiting row the Board
   * paints (the cost tradeoff #572 names): a card nobody opens costs nothing
   * beyond the row already on screen. The one deliberate exception is a
   * decision the durable journal already holds uncertain (`checkPendingForNode`):
   * that card opens and reads the graph itself, because a person left mid
   * decision needs to see it, not find it collapsed again.
   */
  export let run: RunV3;
  export let cockpitApi: CockpitApi;
  export let mutationJournal: MutationJournal;
  export let onRunRead: (run: AnyRun) => void;
  export let navigate: (path: string) => void;

  type GraphLookup =
    | { state: "idle" }
    | { state: "loading" }
    | { state: "ready"; kind: "boolean" | "enum" | "free"; values: readonly string[] }
    | { state: "failed" };

  let expanded = false;
  let graph: GraphLookup = { state: "idle" };
  let pendingWait: WaitMutation | null = null;
  let waitAccepted = false;
  let waitBusy = false;
  let waitFailureMessage: string | null = null;

  $: pendingAnswer = pendingWait === null ? null : waitAnswerText(pendingWait);
  $: confirmedDecision =
    pendingAnswer === null || graph.state !== "ready"
      ? null
      : confirmedDecisionLabel(graph.kind, pendingAnswer, runPageCopy.answerYes, runPageCopy.answerNo);

  // Reading the durable journal is a local, free check (never the network):
  // a decision begun on the run page and left uncertain shows here too,
  // instead of this card offering the same node a second time. Guarded by
  // node identity (the same key-gate `V3RunView` uses for its own wait
  // reads) so a run update that leaves the node unchanged never repeats it,
  // and a genuine move to the run's next wait node drops the stale card.
  let checkedNodeKey = "";
  $: void checkPendingForNode(run.public_run_reference, run.workflow_revision_hash, run.current_node_id);

  async function checkPendingForNode(
    publicRunReference: string,
    workflowRevisionHash: string,
    nodeId: string
  ): Promise<void> {
    const key = `${publicRunReference}:${nodeId}`;
    if (key === checkedNodeKey) return;
    checkedNodeKey = key;
    graph = { state: "idle" };
    pendingWait = null;
    waitAccepted = false;
    waitFailureMessage = null;
    expanded = false;
    const lookup = await loadPendingWaitAnswer(
      mutationJournal,
      publicRunReference,
      workflowRevisionHash,
      nodeId
    );
    if (checkedNodeKey !== key) return;
    if (lookup.kind === "corrupt") {
      // A saved entry this surface cannot trust names no schema kind either
      // -- the card opens on the honest fallback (below) plus this message,
      // never a guess at buttons for a mismatched or foreign identity.
      waitFailureMessage = lookup.message;
      expanded = true;
      return;
    }
    if (lookup.kind === "none") return;
    pendingWait = lookup.pending;
    waitAccepted = false;
    expanded = true;
    if (graph.state === "idle") await loadGraph();
  }

  async function toggle(): Promise<void> {
    expanded = !expanded;
    if (expanded && graph.state === "idle") await loadGraph();
  }

  async function loadGraph(): Promise<void> {
    graph = { state: "loading" };
    try {
      const revision = await cockpitApi.getWorkflowRevision(run.workflow_revision_hash);
      if (
        revision.workflow_revision_hash !== run.workflow_revision_hash ||
        revision.graph.workflow_format_version !== 3
      ) {
        graph = { state: "failed" };
        return;
      }
      const schema = revision.graph.wait_answer_schemas.find(
        (entry) => entry.node_id === run.current_node_id
      );
      graph = {
        state: "ready",
        kind: schema?.kind ?? "free",
        values: schema?.values ?? []
      };
    } catch {
      graph = { state: "failed" };
    }
  }

  async function decide(answer: string): Promise<void> {
    waitFailureMessage = null;
    waitBusy = true;
    try {
      const mutation = await prepareWaitAnswer(
        mutationJournal,
        run.public_run_reference,
        run.workflow_revision_hash,
        run.current_node_id,
        answer
      );
      pendingWait = mutation;
      waitAccepted = false;
      await settle(mutation);
    } catch (error) {
      waitFailureMessage = humanErrorMessage(error, "The answer could not be confirmed.");
    } finally {
      waitBusy = false;
    }
  }

  async function retry(): Promise<void> {
    if (pendingWait === null) return;
    waitFailureMessage = null;
    waitBusy = true;
    try {
      await settle(pendingWait);
    } finally {
      waitBusy = false;
    }
  }

  async function discard(): Promise<void> {
    if (pendingWait === null) return;
    await mutationJournal.discard(pendingWait.mutation_id);
    pendingWait = null;
    waitAccepted = false;
    waitFailureMessage = null;
  }

  async function settle(mutation: WaitMutation): Promise<void> {
    const outcome = await deliverWaitAnswer(
      cockpitApi,
      mutationJournal,
      mutation,
      "The exact retry could not be confirmed."
    );
    if (outcome.kind === "confirmed") {
      pendingWait = null;
      waitAccepted = false;
      onRunRead(outcome.run);
      return;
    }
    if (outcome.kind === "uncertain") {
      pendingWait = outcome.pending;
      waitAccepted = true;
      onRunRead(outcome.run);
      return;
    }
    pendingWait = outcome.pending;
    waitAccepted = false;
    waitFailureMessage = outcome.message;
    // A refusal the journal could not keep uncertain (`pending === null`) is
    // the server saying this node already moved on -- the card must show
    // that truth next, not keep offering an answer the run no longer waits
    // for (operator ruling: no lie on the Board card, #572).
    if (outcome.pending === null) await refreshCanonicalRun();
  }

  async function refreshCanonicalRun(): Promise<void> {
    try {
      onRunRead(await cockpitApi.getRun(run.public_run_reference));
    } catch {
      // The failure message already on screen names the problem; a second,
      // failed refresh attempt would only repeat it.
    }
  }

  function openRun(event: Event): void {
    event.preventDefault();
    navigate(runPath(run.public_run_reference));
  }
</script>

<div class="board-answer">
  <button
    type="button"
    class="board-answer-toggle"
    aria-expanded={expanded}
    data-studio-question={studioQuestions.answerHere.id}
    onclick={() => { void toggle(); }}
  >{wrapDisplayCopy(studioPageCopy.answerHere)}<span aria-hidden="true">{expanded ? " ▴" : " ▾"}</span></button>

  {#if expanded}
    <div class="board-answer-panel" role="group" aria-label={wrapDisplayCopy(studioPageCopy.answerHere)}>
      {#snippet failureAlert(label: string)}
        {#if waitFailureMessage !== null}
          <div class="board-answer-alert" role="alert" aria-label={label}>
            <strong>{label}</strong>
            <small>{waitFailureMessage}</small>
          </div>
        {/if}
      {/snippet}

      {#if pendingWait !== null}
        <p class="board-answer-status" role="status">
          {waitBusy ? "Sending answer" : waitAccepted ? "Answer pending" : "Answer uncertain"}
        </p>
        {@render failureAlert("Send uncertain")}
        <output class="board-answer-value" aria-label="Exact answer"
          >{confirmedDecision !== null
            ? `${wrapDisplayCopy(runPageCopy.answeredPrefix)} ${confirmedDecision}`
            : pendingAnswer}</output
        >
        {#if !waitAccepted && !waitBusy}
          <div class="board-answer-actions">
            <button
              type="button"
              data-studio-question={studioQuestions.answerDecision.id}
              onclick={() => { void retry(); }}
            >Retry</button>
            <button
              type="button"
              class="quiet"
              data-studio-question={studioQuestions.answerDecision.id}
              onclick={() => { void discard(); }}
            >Discard</button>
          </div>
        {/if}
      {:else if graph.state === "loading"}
        <p class="board-answer-status" role="status">{wrapDisplayCopy(studioPageCopy.answerHereLooking)}</p>
        {@render failureAlert("Send failed")}
      {:else if graph.state === "failed"}
        <p class="board-answer-status">{wrapDisplayCopy(studioPageCopy.answerHereUnavailable)}</p>
        {@render failureAlert("Send failed")}
        <a href={runPath(run.public_run_reference)} onclick={openRun}>{wrapDisplayCopy(studioPageCopy.openToAnswer)}</a>
      {:else if graph.state === "ready" && (graph.kind === "boolean" || graph.kind === "enum")}
        <div class="board-answer-buttons" role="group" aria-label={wrapDisplayCopy(runPageCopy.answerLabel)}>
          {#if graph.kind === "boolean"}
            <button
              type="button"
              class="primary"
              disabled={waitBusy}
              data-studio-question={studioQuestions.answerDecision.id}
              onclick={() => { void decide("true"); }}
            >{wrapDisplayCopy(runPageCopy.answerYes)}</button>
            <button
              type="button"
              class="primary"
              disabled={waitBusy}
              data-studio-question={studioQuestions.answerDecision.id}
              onclick={() => { void decide("false"); }}
            >{wrapDisplayCopy(runPageCopy.answerNo)}</button>
          {:else}
            {#each graph.values as value (value)}
              <button
                type="button"
                class="primary"
                disabled={waitBusy}
                data-studio-question={studioQuestions.answerDecision.id}
                onclick={() => { void decide(value); }}
              >{decisionLabel(value)}</button>
            {/each}
          {/if}
        </div>
        {@render failureAlert("Send failed")}
      {:else}
        <p class="board-answer-status">{wrapDisplayCopy(studioPageCopy.needsWrittenAnswer)}</p>
        {@render failureAlert("Send failed")}
        <a href={runPath(run.public_run_reference)} onclick={openRun}>{wrapDisplayCopy(studioPageCopy.openToAnswer)}</a>
      {/if}
    </div>
  {/if}
</div>

<style>
  /* The reveal belongs to the row above it: it lines up with the card's own
     text, not with the surface edge. */
  .board-answer {
    min-width: 0;
    padding-inline: var(--space-4);
  }

  /* The same quiet reveal the rest of the house uses: a disclosure shows
     something, it does not start something, so it wears no card of its own. */
  .board-answer-toggle {
    /* Quiet to look at, but still a finger's worth of target. */
    min-height: var(--tap);
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--signal-attention);
    font-size: var(--text-xs);
    font-weight: var(--weight-strong);
    text-decoration: underline;
    text-underline-offset: var(--underline-offset);
  }

  .board-answer-toggle:hover {
    color: var(--ink);
  }

  .board-answer-panel {
    display: grid;
    gap: var(--space-2);
    margin-top: var(--space-2);
    padding: var(--space-3);
    border: var(--edge) solid var(--line);
    border-radius: var(--r-lg);
    background: var(--panel2);
  }

  .board-answer-status {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-sm);
  }

  .board-answer-value {
    font-size: var(--text-sm);
    overflow-wrap: anywhere;
  }

  .board-answer-buttons,
  .board-answer-actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .board-answer-alert {
    display: grid;
    gap: var(--space-1);
    padding: var(--space-2) var(--space-3);
    border-left: var(--edge-mark) solid var(--signal-failure);
    border-radius: var(--r);
    background: color-mix(in srgb, var(--signal-failure) var(--wash), var(--panel2));
    font-size: var(--text-xs);
  }
</style>
