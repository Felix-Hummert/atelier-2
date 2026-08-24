<script lang="ts">
  import type { AnyRun, CockpitApi, RunV3 } from "../api/client";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { decodeUtf8Base64 } from "../lib/exactBytes";
  import { humanErrorMessage } from "../lib/humanRefusal";
  import { waitAnswerText, type MutationJournal, type WaitMutation } from "../lib/mutationJournal";
  import { runPageCopy } from "../lib/runPageCopy";
  import { runPath } from "../lib/route";
  import {
    deliverWaitAnswer,
    loadPendingWaitAnswer,
    prepareWaitAnswer
  } from "../lib/waitAnswerDelivery";
  import { confirmedDecisionLabel, decisionLabel } from "../lib/waitDecision";
  import { workbenchPageCopy } from "../lib/workbenchPageCopy";

  /**
   * One open decision, pinned so it cannot scroll away in the Workbench stream
   * (issue #580, from the lived failure mode: a decision request once got lost
   * as the conversation grew). It is the decision-as-stage HEART names: the
   * question is the headline, the honest buttons stand under it, and one quiet
   * door leads to the whole run.
   *
   * The answer travels the one audited path `waitAnswerDelivery.ts` owns -- the
   * same path the run page's composer and the Board's inline card use, this the
   * third surface to consume it, so a decision made here carries the identical
   * durable pending/uncertain/retry journal and conflict handling rather than a
   * second implementation. A boolean or enum wait is answered by a click whose
   * exact JSON the click decides; every other shape (a free/written answer)
   * links to the run page, where the composer for prose already lives.
   */
  export let run: RunV3;
  export let workflowName: string;
  export let cockpitApi: CockpitApi;
  export let mutationJournal: MutationJournal;
  export let onRunRead: (run: AnyRun) => void;
  export let navigate: (path: string) => void;

  type QuestionLookup =
    | { state: "loading" }
    | { state: "present"; text: string }
    | { state: "missing" }
    | { state: "failed" };

  type GraphLookup =
    | { state: "loading" }
    | { state: "ready"; kind: "boolean" | "enum" | "free"; values: readonly string[] }
    | { state: "failed" };

  let question: QuestionLookup = { state: "loading" };
  let graph: GraphLookup = { state: "loading" };
  let pendingWait: WaitMutation | null = null;
  let waitAccepted = false;
  let waitBusy = false;
  let waitFailureMessage: string | null = null;

  $: pendingAnswer = pendingWait === null ? null : waitAnswerText(pendingWait);
  $: confirmedDecision =
    pendingAnswer === null || graph.state !== "ready"
      ? null
      : confirmedDecisionLabel(graph.kind, pendingAnswer, runPageCopy.answerYes, runPageCopy.answerNo);

  // One load per waiting node, guarded by node identity the same way the run
  // page and the Board guard theirs: a run update that leaves this node
  // unchanged never re-reads, and a move to another node reloads honestly.
  let loadedNodeKey = "";
  $: void loadForNode(run.public_run_reference, run.workflow_revision_hash, run.current_node_id);

  async function loadForNode(
    publicRunReference: string,
    workflowRevisionHash: string,
    nodeId: string
  ): Promise<void> {
    const key = `${publicRunReference}:${nodeId}`;
    if (key === loadedNodeKey) return;
    loadedNodeKey = key;
    question = { state: "loading" };
    graph = { state: "loading" };
    pendingWait = null;
    waitAccepted = false;
    waitFailureMessage = null;
    await Promise.all([
      loadQuestion(publicRunReference, nodeId, key),
      loadGraph(workflowRevisionHash, nodeId, key),
      loadPending(publicRunReference, workflowRevisionHash, nodeId, key)
    ]);
  }

  async function loadQuestion(publicRunReference: string, nodeId: string, key: string): Promise<void> {
    try {
      const asked = await cockpitApi.getNodeDetail(publicRunReference, nodeId);
      if (key !== loadedNodeKey) return;
      if (asked.job_base64 === null || asked.job_base64.length === 0) {
        question = { state: "missing" };
        return;
      }
      const text = decodeUtf8Base64(asked.job_base64);
      question = text === null || text.length === 0 ? { state: "missing" } : { state: "present", text };
    } catch {
      if (key === loadedNodeKey) question = { state: "failed" };
    }
  }

  async function loadGraph(workflowRevisionHash: string, nodeId: string, key: string): Promise<void> {
    try {
      const revision = await cockpitApi.getWorkflowRevision(workflowRevisionHash);
      if (key !== loadedNodeKey) return;
      if (
        revision.workflow_revision_hash !== workflowRevisionHash ||
        revision.graph.workflow_format_version !== 3
      ) {
        graph = { state: "failed" };
        return;
      }
      const schema = revision.graph.wait_answer_schemas.find((entry) => entry.node_id === nodeId);
      graph = { state: "ready", kind: schema?.kind ?? "free", values: schema?.values ?? [] };
    } catch {
      if (key === loadedNodeKey) graph = { state: "failed" };
    }
  }

  async function loadPending(
    publicRunReference: string,
    workflowRevisionHash: string,
    nodeId: string,
    key: string
  ): Promise<void> {
    const lookup = await loadPendingWaitAnswer(
      mutationJournal,
      publicRunReference,
      workflowRevisionHash,
      nodeId
    );
    if (key !== loadedNodeKey) return;
    if (lookup.kind === "corrupt") {
      waitFailureMessage = lookup.message;
      return;
    }
    if (lookup.kind === "found") {
      pendingWait = lookup.pending;
      waitAccepted = false;
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
    // A refusal the journal could not keep uncertain (the run already moved on)
    // must show that truth next, not keep offering an answer the run no longer
    // waits for -- the same no-lie rule the Board card holds to (#572).
    if (outcome.pending === null) await refreshCanonicalRun();
  }

  async function refreshCanonicalRun(): Promise<void> {
    try {
      onRunRead(await cockpitApi.getRun(run.public_run_reference));
    } catch {
      // The failure message already on screen names the problem; a second
      // failed refresh would only repeat it.
    }
  }

  function openRun(event: Event): void {
    event.preventDefault();
    navigate(runPath(run.public_run_reference));
  }
</script>

<section
  class="pinned-decision"
  class:pinned-decision-sent={pendingWait !== null}
  aria-labelledby="pinned-decision-title-{run.public_run_reference}"
>
  <p class="from">
    <b>{workflowName}</b>
    {wrapDisplayCopy(workbenchPageCopy.waitingFrom)}
  </p>

  {#if pendingWait !== null}
    <h3 id="pinned-decision-title-{run.public_run_reference}" class="question">
      {waitBusy ? "Sending answer" : waitAccepted ? "Answer pending" : "Answer uncertain"}
    </h3>
    {#if waitFailureMessage !== null}
      <div class="pinned-alert" role="alert" aria-label="Send uncertain">
        <strong>Send uncertain</strong>
        <small>{waitFailureMessage}</small>
      </div>
    {/if}
    <output class="pinned-answer" aria-label="Exact answer"
      >{confirmedDecision !== null
        ? `${wrapDisplayCopy(runPageCopy.answeredPrefix)} ${confirmedDecision}`
        : pendingAnswer}</output
    >
    {#if !waitAccepted && !waitBusy}
      <div class="pinned-actions">
        <button type="button" onclick={() => { void retry(); }}>Retry</button>
        <button type="button" class="quiet" onclick={() => { void discard(); }}>Discard</button>
      </div>
    {/if}
  {:else}
    {#if question.state === "present"}
      <h3 id="pinned-decision-title-{run.public_run_reference}" class="question">{question.text}</h3>
    {:else if question.state === "missing"}
      <h3 id="pinned-decision-title-{run.public_run_reference}" class="question">{wrapDisplayCopy(runPageCopy.questionMissing)}</h3>
    {:else if question.state === "failed"}
      <h3 id="pinned-decision-title-{run.public_run_reference}" class="question">{wrapDisplayCopy(runPageCopy.needsYou)}</h3>
    {:else}
      <h3 id="pinned-decision-title-{run.public_run_reference}" class="question looking">{wrapDisplayCopy(runPageCopy.questionLooking)}</h3>
    {/if}

    {#if graph.state === "loading"}
      <p class="pinned-status" role="status">{wrapDisplayCopy(runPageCopy.questionLooking)}</p>
    {:else if graph.state === "ready" && graph.kind === "boolean"}
      <div class="pinned-buttons" role="group" aria-label={wrapDisplayCopy(runPageCopy.answerLabel)}>
        <button class="primary" type="button" disabled={waitBusy} onclick={() => { void decide("true"); }}>{wrapDisplayCopy(runPageCopy.answerYes)}</button>
        <button class="primary" type="button" disabled={waitBusy} onclick={() => { void decide("false"); }}>{wrapDisplayCopy(runPageCopy.answerNo)}</button>
      </div>
    {:else if graph.state === "ready" && graph.kind === "enum"}
      <div class="pinned-buttons" role="group" aria-label={wrapDisplayCopy(runPageCopy.answerLabel)}>
        {#each graph.values as value (value)}
          <button class="primary" type="button" disabled={waitBusy} onclick={() => { void decide(value); }}>{decisionLabel(value)}</button>
        {/each}
      </div>
    {/if}

    {#if waitFailureMessage !== null}
      <div class="pinned-alert" role="alert" aria-label="Send failed">
        <strong>Send failed</strong>
        <small>{waitFailureMessage}</small>
      </div>
    {/if}
  {/if}

  <!-- The one quiet door to the whole run: the story behind the question, and
       the only place a free/written answer is composed. Deliberately
       subordinate to the buttons above, never a second answer control of equal
       weight (Leonardo-Gate 23.08.). -->
  <a class="pinned-door" href={runPath(run.public_run_reference)} onclick={openRun}
    >{graph.state === "ready" && graph.kind === "free"
      ? wrapDisplayCopy(workbenchPageCopy.openTheRun)
      : wrapDisplayCopy(workbenchPageCopy.openTheRunForStory)} →</a
  >
</section>

<style>
  .pinned-decision {
    display: grid;
    gap: var(--space-3);
    border: var(--edge-strong) solid var(--signal-attention-mark);
    border-radius: var(--r-lg);
    padding: var(--space-5);
    background: var(--panel2);
  }

  .pinned-decision-sent {
    border-color: var(--signal-live);
  }

  .from {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-2xs);
    font-weight: var(--weight-heavy);
    letter-spacing: var(--tracking-label);
    text-transform: uppercase;
  }

  .from b {
    color: var(--signal-attention);
  }

  .pinned-decision-sent .from b {
    color: var(--signal-live);
  }

  .question {
    margin: 0;
    font-size: var(--text-lg);
    line-height: var(--leading-tight);
    overflow-wrap: anywhere;
  }

  .question.looking {
    color: var(--ink-dim);
  }

  .pinned-status {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-sm);
  }

  .pinned-answer {
    font-size: var(--text-sm);
    overflow-wrap: anywhere;
  }

  .pinned-buttons,
  .pinned-actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-3);
  }

  .pinned-alert {
    display: grid;
    gap: var(--space-1);
    padding: var(--space-2) var(--space-3);
    border-left: var(--edge-mark) solid var(--signal-failure);
    border-radius: var(--r);
    background: color-mix(in srgb, var(--signal-failure) var(--wash), var(--panel2));
    font-size: var(--text-xs);
  }

  .pinned-door {
    justify-self: start;
    color: var(--ink-dim);
    font-size: var(--text-xs);
    font-weight: var(--weight-medium);
  }

  .pinned-door:hover {
    color: var(--ink);
  }
</style>
