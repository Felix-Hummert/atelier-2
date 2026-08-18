<script lang="ts">
  import { onMount, tick } from "svelte";

  import {
    CockpitRequestError,
    isRunV3,
    type CockpitApi,
    type NodeDetail,
    type RunV3,
    type WorkflowRevisionDetail
  } from "../api/client";
  import { humanErrorMessage } from "../lib/humanRefusal";
  import {
    MutationJournal,
    v3WaitMutation,
    waitAnswerText,
    waitMutationId,
    type JournalEntry,
    type WaitMutation
  } from "../lib/mutationJournal";
  import type { StreamProjection } from "../lib/runProjection";
  import NodeDetailPanel from "./NodeDetailPanel.svelte";
  import ProblemNotice from "./ProblemNotice.svelte";
  import StateMark from "./StateMark.svelte";
  import V3AnswerCard from "./V3AnswerCard.svelte";
  import WorkflowGraphDrawing from "./WorkflowGraphDrawing.svelte";

  export let run: RunV3;
  export let cockpitApi: CockpitApi;
  export let mutationJournal: MutationJournal;
  export let projection: StreamProjection | null = null;
  export let onRunRead: (run: RunV3) => void = () => {};

  const PREVIEW_CHARACTERS = 120;

  /**
   * What arrived while the operator was watching, newest last.
   *
   * Only the two kinds that say a node is done with its turn: a completion and a
   * failure. The rest of the stream is real and is kept by the projection; what
   * belongs on this line is the answer to "is it moving", not the whole record.
   * The full output is the panel's (#238) -- here it is a preview, because a
   * page that pasted a whole agent answer into the timeline would bury the very
   * movement it exists to show.
   */
  $: arrived = (projection?.events ?? []).filter(
    (event) => event.event === "AGENT_COMPLETED" || event.event === "AGENT_FAILED"
  );

  function preview(cursor: string): string | null {
    const output = projection?.agent_outputs_by_cursor.get(cursor);
    if (output === undefined || output.kind === "empty") return null;
    const text = output.kind === "utf8" ? output.value : `${output.byte_count} bytes`;
    return text.length > PREVIEW_CHARACTERS
      ? `${text.slice(0, PREVIEW_CHARACTERS)}…`
      : text;
  }

  let openNodeId: string | null = null;
  let detail: NodeDetail | null = null;
  let failure: string | null = null;
  let pendingWait: Extract<JournalEntry, { kind: "wait" }> | null = null;
  let waitAccepted = false;
  let waitBusy = false;
  let waitValidationMessage: string | null = null;
  let waitFailureMessage: string | null = null;
  let answerCard: V3AnswerCard;
  $: pendingAnswer = pendingWait === null ? null : waitAnswerText(pendingWait);
  $: waiting = run.state === "WAITING_INPUT";
  type WaitQuestion =
    | { kind: "loading" }
    | { kind: "present"; text: string }
    | { kind: "absent" }
    | { kind: "failed"; message: string };
  let waitQuestion: WaitQuestion = { kind: "loading" };

  /**
   * One click asks the server, and the server answers the whole node.
   *
   * The panel deliberately does not assemble itself from the run, the events and
   * the receipts the page already holds: those are three sources for one answer,
   * and the derivation is exactly what the node read exists to end.
   */
  async function openNode(nodeId: string): Promise<void> {
    if (openNodeId === nodeId) {
      closeNode();
      return;
    }
    openNodeId = nodeId;
    detail = null;
    failure = null;
    try {
      const answered = await cockpitApi.getNodeDetail(run.public_run_reference, nodeId);
      if (openNodeId === nodeId) {
        detail = answered;
      }
    } catch (error) {
      if (openNodeId === nodeId) {
        failure = error instanceof Error ? error.message : String(error);
      }
    }
  }

  function closeNode(): void {
    openNodeId = null;
    detail = null;
    failure = null;
  }

  /**
   * The rail still owns state. The published excerpt owns the shape.
   *
   * A V1 or V2 run is folded here in the browser because its events arrive one
   * at a time and the page has to keep up. A V3 run carries the rail the server
   * already walked; recomputing that order here would be a second owner. The
   * drawing reads `depends_on` from the published excerpt and paints each
   * node's state from the rail — two facts, one picture.
   */
  $: rail = run.node_rail;

  type GraphRequest =
    | { state: "loading" }
    | { state: "ready"; previews: Extract<WorkflowRevisionDetail["graph"], { workflow_format_version: 3 }>["node_previews"] }
    | { state: "failed"; message: string };

  let graphRequest: GraphRequest = { state: "loading" };

  onMount(() => {
    void loadGraph();
    void loadPendingWait();
  });

  $: if (waiting) void loadWaitQuestion();

  type DecodedWaitJob =
    | { kind: "missing" }
    | { kind: "present"; text: string }
    | { kind: "corrupt" };

  function decodedWaitJob(jobBase64: string | null): DecodedWaitJob {
    if (jobBase64 === null || jobBase64.length === 0) return { kind: "missing" };
    try {
      const text = new TextDecoder("utf-8", { fatal: true }).decode(
        Uint8Array.from(atob(jobBase64), (character) => character.charCodeAt(0))
      );
      return text.length === 0 ? { kind: "missing" } : { kind: "present", text };
    } catch {
      return { kind: "corrupt" };
    }
  }

  let waitQuestionKey = "";

  async function loadWaitQuestion(): Promise<void> {
    if (run.state !== "WAITING_INPUT") {
      waitQuestion = { kind: "absent" };
      waitQuestionKey = "";
      return;
    }
    const key = `${run.public_run_reference}:${run.current_node_id}`;
    if (key === waitQuestionKey) return;
    waitQuestionKey = key;
    waitQuestion = { kind: "loading" };
    try {
      const detail = await cockpitApi.getNodeDetail(
        run.public_run_reference,
        run.current_node_id
      );
      const decoded = decodedWaitJob(detail.job_base64);
      if (decoded.kind === "present") {
        waitQuestion = { kind: "present", text: decoded.text };
      } else if (decoded.kind === "corrupt") {
        waitQuestion = {
          kind: "failed",
          message: "The wait question could not be read."
        };
      } else {
        waitQuestion = { kind: "absent" };
      }
    } catch (error) {
      waitQuestion = {
        kind: "failed",
        message: humanErrorMessage(error, "The wait question could not be read.")
      };
    }
  }

  async function loadPendingWait(): Promise<void> {
    if (run.state !== "WAITING_INPUT") {
      pendingWait = null;
      waitAccepted = false;
      return;
    }
    const mutationId = waitMutationId(run.public_run_reference, run.current_node_id);
    const entry = await mutationJournal.get(mutationId);
    if (entry !== null && entry.kind !== "wait") {
      waitFailureMessage = "The saved request identity belongs to another operation.";
      return;
    }
    if (entry === null) {
      pendingWait = null;
      waitAccepted = false;
      return;
    }
    if (
      entry.workflow_revision_hash !== run.workflow_revision_hash ||
      entry.node_id !== run.current_node_id ||
      entry.public_run_reference !== run.public_run_reference
    ) {
      waitFailureMessage = "The saved exact answer does not belong to this waiting node.";
      return;
    }
    if (pendingWait?.mutation_id !== entry.mutation_id) waitAccepted = false;
    pendingWait = entry;
  }

  async function submitWait(answer: string): Promise<void> {
    waitValidationMessage = null;
    waitFailureMessage = null;
    if (answer.trim().length === 0) {
      waitValidationMessage = "Enter an answer.";
      return;
    }
    if (run.state !== "WAITING_INPUT") return;
    waitBusy = true;
    let mutation: WaitMutation | null = null;
    try {
      mutation = await v3WaitMutation(
        run.public_run_reference,
        run.workflow_revision_hash,
        run.current_node_id,
        answer
      );
      const prepared = await mutationJournal.prepare(mutation);
      if (prepared.kind !== "wait") throw new Error("The exact request has the wrong kind.");
      pendingWait = prepared;
      waitAccepted = false;
      await deliverWait(mutation);
      await focusAfterDelivery();
    } catch (error) {
      if (mutation !== null) await recordWaitFailure(mutation.mutation_id, error);
      waitFailureMessage = humanErrorMessage(error, "The answer could not be confirmed.");
      await focusWaitFailure();
    } finally {
      waitBusy = false;
    }
  }

  async function retryWait(): Promise<void> {
    if (pendingWait === null) return;
    waitFailureMessage = null;
    waitBusy = true;
    try {
      await deliverWait(pendingWait);
      await focusAfterDelivery();
    } catch (error) {
      await recordWaitFailure(pendingWait.mutation_id, error);
      waitFailureMessage = humanErrorMessage(error, "The exact retry could not be confirmed.");
      await focusWaitFailure();
    } finally {
      waitBusy = false;
    }
  }

  async function discardWait(): Promise<void> {
    if (pendingWait === null) return;
    await mutationJournal.discard(pendingWait.mutation_id);
    pendingWait = null;
    waitAccepted = false;
    waitFailureMessage = null;
    await tick();
    answerCard?.focusInput();
  }

  async function deliverWait(mutation: WaitMutation): Promise<void> {
    const result = await cockpitApi.answer(mutation);
    const resolved = await mutationJournal.resolve(mutation.mutation_id, {
      type: "wait_response",
      status: result.status,
      target: mutation.target,
      request_body_base64: mutation.body_base64
    });
    if (result.status === 200 && !resolved) {
      throw new Error("The answer response did not prove the exact request.");
    }
    if (result.status === 202 && resolved) {
      throw new Error("A pending answer was incorrectly treated as durable completion.");
    }
    if (!isRunV3(result.value)) {
      throw new Error("The answer response was not a V3 run.");
    }
    if (result.value.public_run_reference !== run.public_run_reference) {
      throw new CockpitRequestError("The API returned a different durable run.");
    }
    onRunRead(result.value);
    if (result.status === 202) {
      const uncertain = await mutationJournal.markUncertain(mutation.mutation_id);
      if (uncertain.kind !== "wait") throw new Error("The accepted request changed kind.");
      pendingWait = uncertain;
      waitAccepted = true;
    } else {
      pendingWait = null;
      waitAccepted = false;
    }
  }

  async function recordWaitFailure(mutationId: string, error: unknown): Promise<void> {
    if (error instanceof CockpitRequestError && error.definitive_failure) {
      await mutationJournal.discard(mutationId);
      pendingWait = null;
      waitAccepted = false;
      return;
    }
    if (await mutationJournal.get(mutationId)) {
      await mutationJournal.markUncertain(mutationId);
      const entry = await mutationJournal.get(mutationId);
      pendingWait = entry?.kind === "wait" ? entry : pendingWait;
    }
  }

  async function focusAfterDelivery(): Promise<void> {
    await tick();
    if (pendingWait !== null && waitAccepted) {
      answerCard?.focusStatus();
    }
  }

  async function focusWaitFailure(): Promise<void> {
    await tick();
    if (pendingWait !== null) {
      answerCard?.focusRetry();
    } else {
      answerCard?.focusInput();
    }
  }

  async function loadGraph(): Promise<void> {
    graphRequest = { state: "loading" };
    try {
      const revision = await cockpitApi.getWorkflowRevision(run.workflow_revision_hash);
      if (revision.workflow_revision_hash !== run.workflow_revision_hash) {
        throw new Error("The workflow revision did not match the durable run.");
      }
      if (revision.graph.workflow_format_version !== 3) {
        throw new Error("The bound revision is not a V3 graph.");
      }
      graphRequest = { state: "ready", previews: revision.graph.node_previews };
    } catch (error) {
      graphRequest = {
        state: "failed",
        message: error instanceof Error ? error.message : "The workflow graph could not be read."
      };
    }
  }
</script>

<section class="v3-run" aria-labelledby="v3-run-title">
  <header class="run-header">
    <div>
      <p class="eyebrow">Durable run</p>
      <h1 id="v3-run-title">Run {run.run_id}</h1>
    </div>
    <p class="standing" aria-label="Where this run stands">
      <StateMark
        state={run.state === "COMPLETED" ? "succeeded" : run.state === "FAILED" ? "failed" : "working"}
      />
      <span class="following">
        {#if projection === null || projection.connection === "connecting"}
          Connecting…
        {:else if projection.connection === "complete"}
          Ended
        {:else if projection.connection === "failed"}
          Disconnected
        {:else}
          Following live
        {/if}
      </span>
    </p>
  </header>

  {#if waiting}
    {#if waitQuestion.kind === "failed"}
      <ProblemNotice title="The wait question could not be read" message={waitQuestion.message} />
    {/if}
    <V3AnswerCard
      bind:this={answerCard}
      nodeId={run.current_node_id}
      question={waitQuestion.kind === "present" ? waitQuestion.text : null}
      questionMissing={waitQuestion.kind === "absent"}
      questionFailed={waitQuestion.kind === "failed"}
      pending={pendingWait}
      {pendingAnswer}
      accepted={waitAccepted}
      busy={waitBusy}
      validationMessage={waitValidationMessage}
      failureMessage={waitFailureMessage}
      onAnswer={(answer) => { void submitWait(answer); }}
      onRetry={() => { void retryWait(); }}
      onDiscard={() => { void discardWait(); }}
    />
  {/if}

  {#if graphRequest.state === "loading"}
    <p class="muted" role="status">Looking…</p>
  {:else if graphRequest.state === "failed"}
    <ProblemNotice title="The graph could not be read" message={graphRequest.message} />
    <ol class="rail">
      {#each rail as entry (entry.node_id)}
        <li class="rail-entry" class:current={entry.node_id === run.current_node_id}>
          <button
            type="button"
            class="node-button"
            aria-expanded={openNodeId === entry.node_id}
            on:click={() => void openNode(entry.node_id)}
          >
            <StateMark state={entry.state} />
            <span class="node-id">{entry.node_id}</span>
          </button>
        </li>
      {/each}
    </ol>
  {:else}
    <WorkflowGraphDrawing
      previews={graphRequest.previews}
      {rail}
      currentNodeId={run.current_node_id}
      selectedNodeId={openNodeId}
      onSelect={(nodeId) => { void openNode(nodeId); }}
    />
  {/if}

  {#if openNodeId !== null}
    {#if failure !== null}
      <ProblemNotice title="This node could not be read" message={failure} />
    {:else if detail !== null}
      <NodeDetailPanel {detail} onClose={closeNode} />
    {:else}
      <p class="muted">Reading {openNodeId}…</p>
    {/if}
  {/if}

  <section class="stream">
    <p class="eyebrow">As it happened</p>
    {#if arrived.length === 0}
      <p class="muted">No node has finished its turn yet.</p>
    {:else}
      <ol class="events" aria-label="Events as they arrive">
        {#each arrived as event (event.cursor)}
          <li class="event">
            <StateMark
              state={event.event === "AGENT_COMPLETED" ? "succeeded" : "failed"}
            />
            <span class="node-id">{event.node_id}</span>
            {#if preview(event.cursor) !== null}
              <code class="preview">{preview(event.cursor)}</code>
            {/if}
          </li>
        {/each}
      </ol>
    {/if}
  </section>

  <dl class="facts">
    <dt>Terminal hash</dt>
    <dd>
      {#if run.terminal_hash === null}
        <span class="muted">not yet</span>
      {:else}
        <code>{run.terminal_hash}</code>
      {/if}
    </dd>
    <dt>Run configuration</dt>
    <dd><code>{run.run_configuration_revision_hash}</code></dd>
    <dt>Workflow revision</dt>
    <dd><code>{run.workflow_revision_hash}</code></dd>
  </dl>
</section>

<style>
  .v3-run { display: grid; gap: 1rem; }
  .run-header { display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: baseline; justify-content: space-between; }
  .standing { display: flex; align-items: center; gap: 0.75rem; margin: 0; }
  .following { color: var(--muted); }
  .stream { display: grid; gap: 0.4rem; margin-top: 0.5rem; }
  .events { list-style: none; margin: 0; padding: 0; display: grid; gap: 0.4rem; }
  .event { display: flex; align-items: baseline; gap: 0.6rem; }
  .preview { opacity: 0.8; overflow-wrap: anywhere; }
  .rail { list-style: none; margin: 0; padding: 0; display: grid; gap: 0.4rem; }
  .rail-entry { display: flex; align-items: center; gap: 0.6rem; padding: 0.4rem 0.6rem; border-radius: 0.4rem; }
  .rail-entry.current { background: color-mix(in srgb, currentColor 8%, transparent); }
  .node-button { display: flex; align-items: center; gap: 0.6rem; width: 100%; border: 0; background: transparent; padding: 0; font: inherit; color: inherit; cursor: pointer; text-align: left; }
  .node-id { font-weight: 600; }
  .facts { display: grid; grid-template-columns: auto 1fr; gap: 0.3rem 1rem; margin: 0; }
  .facts dd { margin: 0; overflow-wrap: anywhere; }
  .muted { color: var(--muted); }
</style>
