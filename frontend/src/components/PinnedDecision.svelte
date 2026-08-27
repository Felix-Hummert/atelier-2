<script lang="ts">
  import type { AnyRun, CockpitApi, RunV3 } from "../api/client";
  import { decisionStatusCopy } from "../lib/decisionStatusCopy";
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
  import { workbenchQuestionAttribute, workbenchQuestions } from "../lib/workbenchQuestions";

  /**
   * One open decision, pinned so it cannot scroll away in the Workbench stream
   * (issue #580, from the lived failure mode: a decision request once got lost
   * as the conversation grew). It is the decision-as-stage HEART names: the
   * question is the headline, the honest buttons stand under it, and one quiet
   * door leads to the whole run.
   *
   * The answer travels the one audited path `waitAnswerDelivery.ts` owns -- the
   * same path the run page's composer uses, so a decision made here carries the identical
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
  export let compact = false;
  export let onExpand: () => void;

  type QuestionLookup =
    | { state: "loading" }
    | { state: "present"; text: string }
    | { state: "missing" }
    | { state: "failed" };

  type GraphLookup =
    | { state: "loading" }
    | {
        state: "ready";
        kind: "boolean" | "enum" | "free";
        values: readonly string[];
        role: string | null;
      }
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
  $: senderRole = graph.state === "ready" && graph.role !== null ? graph.role : run.current_node_id;
  $: senderItem = run.orders.length === 0 ? null : run.orders.map((order) => order.name).join(", ");

  // One load per waiting node, guarded by node identity the same way the run
  // page guards its own: a run update that leaves this node
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
      const node = revision.graph.node_previews.find((entry) => entry.id === nodeId);
      graph = {
        state: "ready",
        kind: schema?.kind ?? "free",
        values: schema?.values ?? [],
        role: node?.role ?? null
      };
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
    // waits for.
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
  class:pinned-decision-compact={compact}
  aria-labelledby="pinned-decision-title-{run.public_run_reference}"
>
  {#if compact && pendingWait === null}
    <button
      class="compact-answer"
      type="button"
      {...{ [workbenchQuestionAttribute]: workbenchQuestions.answerDecision.id }}
      onclick={onExpand}
    >
      <span class="from compact-from">
        <b>{senderRole}</b>
        <span> · {workflowName}</span>
        {#if senderItem !== null}
          <span> · {senderItem}</span>
        {/if}
      </span>
      <span id="pinned-decision-title-{run.public_run_reference}" class="compact-question">
        {question.state === "present"
          ? question.text
          : question.state === "missing"
            ? wrapDisplayCopy(runPageCopy.questionMissing)
            : question.state === "failed"
              ? wrapDisplayCopy(runPageCopy.needsYou)
              : wrapDisplayCopy(runPageCopy.questionLooking)}
      </span>
      <span class="compact-action">{wrapDisplayCopy(workbenchPageCopy.answerDecision)}</span>
    </button>
  {:else}
    <p class="from">
      <b>{senderRole}</b>
      <span> · {workflowName}</span>
      {#if senderItem !== null}
        <span> · {senderItem}</span>
      {/if}
    </p>

  {#if pendingWait !== null}
    <h3 id="pinned-decision-title-{run.public_run_reference}" class="question">
      {waitBusy ? decisionStatusCopy.sending : waitAccepted ? decisionStatusCopy.pending : decisionStatusCopy.uncertain}
    </h3>
    {#if waitFailureMessage !== null}
      <div class="pinned-alert" role="alert" aria-label={decisionStatusCopy.sendUncertain}>
        <strong>{decisionStatusCopy.sendUncertain}</strong>
        <small>{waitFailureMessage}</small>
      </div>
    {/if}
    <output class="pinned-answer" aria-label={decisionStatusCopy.exactAnswer}
      >{confirmedDecision !== null
        ? `${wrapDisplayCopy(runPageCopy.answeredPrefix)} ${confirmedDecision}`
        : pendingAnswer}</output
    >
    {#if !waitAccepted && !waitBusy}
      <div class="pinned-actions">
        <button
          type="button"
          {...{ [workbenchQuestionAttribute]: workbenchQuestions.answerDecision.id }}
          onclick={() => { void retry(); }}
        >Retry</button>
        <button
          type="button"
          class="quiet"
          {...{ [workbenchQuestionAttribute]: workbenchQuestions.answerDecision.id }}
          onclick={() => { void discard(); }}
        >Discard</button>
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

    {#if waitFailureMessage !== null}
      <div class="pinned-alert" role="alert" aria-label={decisionStatusCopy.sendFailed}>
        <strong>{decisionStatusCopy.sendFailed}</strong>
        <small>{waitFailureMessage}</small>
      </div>
    {/if}
  {/if}

    <!-- The one quiet door to the whole run: the story behind the question,
         kept as the stage's aside rather than a second decision control. -->
    <div class="pinned-acts">
      {#if pendingWait === null && graph.state === "loading"}
        <p class="pinned-status" role="status">{wrapDisplayCopy(runPageCopy.questionLooking)}</p>
      {:else if pendingWait === null && graph.state === "ready" && graph.kind === "boolean"}
        <div class="pinned-buttons" role="group" aria-label={wrapDisplayCopy(runPageCopy.answerLabel)}>
          <button class="primary" type="button" disabled={waitBusy} {...{ [workbenchQuestionAttribute]: workbenchQuestions.answerDecision.id }} onclick={() => { void decide("true"); }}>{wrapDisplayCopy(runPageCopy.answerYes)}</button>
          <button class="primary" type="button" disabled={waitBusy} {...{ [workbenchQuestionAttribute]: workbenchQuestions.answerDecision.id }} onclick={() => { void decide("false"); }}>{wrapDisplayCopy(runPageCopy.answerNo)}</button>
        </div>
      {:else if pendingWait === null && graph.state === "ready" && graph.kind === "enum"}
        <div class="pinned-buttons" role="group" aria-label={wrapDisplayCopy(runPageCopy.answerLabel)}>
          {#each graph.values as value (value)}
            <button class="primary" type="button" disabled={waitBusy} {...{ [workbenchQuestionAttribute]: workbenchQuestions.answerDecision.id }} onclick={() => { void decide(value); }}>{decisionLabel(value)}</button>
          {/each}
        </div>
      {/if}
      <a class="pinned-door" href={runPath(run.public_run_reference)} onclick={openRun}
        >{wrapDisplayCopy(workbenchPageCopy.openTheRun)}</a
      >
    </div>
  {/if}
</section>

<style>
  .pinned-decision {
    display: grid;
    gap: var(--space-2);
    border: var(--edge-strong) solid var(--signal-attention-mark);
    border-radius: var(--r-lg);
    padding: var(--space-3) var(--space-5);
    background: var(--panel2);
  }

  .pinned-decision-sent {
    border-color: var(--signal-live);
  }

  .pinned-decision-compact {
    border-width: var(--edge);
    padding: 0;
  }

  .compact-answer {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    width: 100%;
    min-height: var(--tap);
    gap: var(--space-2);
    border: 0;
    padding: var(--space-3) var(--space-4);
    background: transparent;
    color: inherit;
    font: inherit;
    font-size: var(--text-sm);
    text-align: left;
  }

  .compact-question {
    flex: 1;
    min-width: var(--decision-question-min);
  }

  .compact-action {
    margin-left: auto;
    color: var(--signal-attention);
    font-size: var(--text-sm);
    font-weight: var(--weight-strong);
    white-space: nowrap;
  }

  .from {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-2xs);
    font-weight: var(--weight-heavy);
  }

  .compact-from {
    flex: 0 1 auto;
    white-space: nowrap;
  }

  .from b {
    color: var(--ink);
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

  .pinned-acts,
  .pinned-buttons,
  .pinned-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
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
    margin-left: auto;
    justify-self: start;
    color: var(--ink-dim);
    font-size: var(--text-xs);
    font-weight: var(--weight-medium);
  }

  .pinned-door:hover {
    color: var(--ink);
  }

  @media (max-width: 48rem) {
    .compact-from {
      white-space: normal;
    }
  }
</style>
