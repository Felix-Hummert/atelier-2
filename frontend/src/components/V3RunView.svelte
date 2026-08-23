<script lang="ts">
  import { onMount, tick } from "svelte";

  import {
    CockpitRequestError,
    isRunV3,
    type CockpitApi,
    type NodeDetail,
    type RunEvent,
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
  import { whenFacts, type StreamProjection } from "../lib/runProjection";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { runPageCopy } from "../lib/runPageCopy";
  import { runStanding, standingMarks, standingWords } from "../lib/runState";
  import { protocolDetail, protocolTitle } from "../lib/streamStatus";
  import { encodeWaitAnswer } from "../lib/waitAnswer";
  import { ageLabel } from "../lib/when";
  import NodeDetailPanel from "./NodeDetailPanel.svelte";
  import ProblemNotice from "./ProblemNotice.svelte";
  import V3AnswerCard, { type WaitContextSource } from "./V3AnswerCard.svelte";
  import WorkflowGraphDrawing from "./WorkflowGraphDrawing.svelte";

  /**
   * The run, in the order a person reads it (operator ruling 23.08.):
   *
   * 1. what this is and where it stands — name, description, one plain state
   *    sentence with its duration, and nothing else;
   * 2. what needs the operator now — the waiting question with the material it
   *    is about, as the one dominant card;
   * 3. the run as a picture — the quiet pipe;
   * 4. everything else only behind a click — node tabs, and every fingerprint
   *    inside the Evidence tab there.
   *
   * An element that fits none of the four does not belong on this page.
   */
  export let run: RunV3;
  export let cockpitApi: CockpitApi;
  export let mutationJournal: MutationJournal;
  export let projection: StreamProjection | null = null;
  export let onRunRead: (run: RunV3) => void = () => {};
  export let onRetryStream: () => void = () => {};

  /**
   * The reason the run stopped, in the words of the owner that refused.
   *
   * The graph draws state and nothing else, so a failure's *why* has exactly
   * one place on the main surface: beside the state sentence that says the run
   * failed. The same words also stand on the node itself, under Result --
   * that is the node's own history, not a second copy of this sentence.
   */
  $: failedReasons = new Map(
    (projection?.events ?? []).flatMap((event) => {
      const reason = storedFailureReason(event);
      return reason === null ? [] : [[event.node_id, reason] as const];
    })
  );
  $: stopped =
    run.state === "FAILED"
      ? ([...failedReasons.entries()][0] ?? null)
      : null;

  function storedFailureReason(event: RunEvent): string | null {
    if (event.event !== "AGENT_FAILED") return null;
    if (!("reason" in event) || event.reason === null || event.reason === "") {
      return null;
    }
    return event.reason;
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
  $: standing = runStanding(run.state);

  /**
   * The state sentence keeps its relative words ("Done 2 min ago"); the exact
   * facts line beneath it is what used to hide behind an "Exact time" reveal
   * link (operator ruling 23.08.: always-visible facts, not a link a person
   * has to find first). A missing timestamp drops its own fact rather than
   * showing a placeholder.
   */
  $: relativeStanding =
    run.started_at == null
      ? null
      : ageLabel(
          run.started_at,
          new Date(),
          run.ended_at == null ? "for" : "ago",
          run.ended_at ?? undefined
        );
  $: runFacts = whenFacts(run.started_at ?? null, run.ended_at ?? null, new Date());
  $: runFactLine = [
    runFacts.startedExact === null
      ? null
      : `${wrapDisplayCopy(runPageCopy.started)} ${runFacts.startedExact}`,
    runFacts.endedExact === null
      ? null
      : `${wrapDisplayCopy(runPageCopy.ended)} ${runFacts.endedExact}`,
    runFacts.durationWords === null
      ? null
      : `${wrapDisplayCopy(runPageCopy.duration)} ${runFacts.durationWords}`
  ]
    .filter((part): part is string => part !== null)
    .join(" · ");
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
   * A V3 run carries the rail the server already walked; recomputing that order
   * here would be a second owner. The drawing reads `depends_on` from the
   * published excerpt and paints each node's state from the rail — two facts,
   * one picture.
   */
  $: rail = run.node_rail;

  type GraphRequest =
    | { state: "loading" }
    | {
        state: "ready";
        name: string;
        description: string | null;
        previews: Extract<WorkflowRevisionDetail["graph"], { workflow_format_version: 3 }>["node_previews"];
        loops: Extract<WorkflowRevisionDetail["graph"], { workflow_format_version: 3 }>["loops"];
        waitAnswerSchemas: Extract<
          WorkflowRevisionDetail["graph"],
          { workflow_format_version: 3 }
        >["wait_answer_schemas"];
      }
    | { state: "failed"; message: string };

  /**
   * A V3 document always declares a name once read (the graph schema requires
   * one), so this view never has a permanently unnamed workflow to fall back
   * to the way a V1 or V2 revision does. What it has instead is a name still
   * arriving or a graph that could not be read -- and the title says which,
   * rather than calling either one "Unnamed".
   */
  $: headerTitle =
    graphRequest.state === "ready"
      ? graphRequest.name
      : graphRequest.state === "loading"
        ? "Looking…"
        : "Workflow unavailable";
  $: description = graphRequest.state === "ready" ? graphRequest.description : null;

  let graphRequest: GraphRequest = { state: "loading" };

  onMount(() => {
    void loadGraph();
    void loadPendingWait();
  });

  $: if (waiting) void loadWaitQuestion();
  $: if (waiting && graphRequest.state === "ready") void loadWaitContext();

  /** Every earlier node the published document says this one reads. */
  function readsFrom(nodeId: string): readonly string[] {
    if (graphRequest.state !== "ready") return [];
    return graphRequest.previews.find((preview) => preview.id === nodeId)?.depends_on ?? [];
  }

  /** The waiting node's own answer schema, or `free` where the graph has not arrived yet. */
  $: currentWaitAnswerSchema =
    graphRequest.state === "ready"
      ? (graphRequest.waitAnswerSchemas.find(
          (entry) => entry.node_id === run.current_node_id
        ) ?? null)
      : null;

  function decodedText(base64: string): string | null {
    try {
      return new TextDecoder("utf-8", { fatal: true }).decode(
        Uint8Array.from(atob(base64), (character) => character.charCodeAt(0))
      );
    } catch {
      return null;
    }
  }

  let waitSources: readonly WaitContextSource[] = [];
  let waitSourcesLoading = false;
  let waitContextKey = "";

  /**
   * The material the pending question is about: what every step this one reads
   * actually wrote. Without it the operator is asked to judge something he
   * cannot see (operator, 23.08.).
   */
  async function loadWaitContext(): Promise<void> {
    const key = `${run.public_run_reference}:${run.current_node_id}`;
    if (key === waitContextKey) return;
    waitContextKey = key;
    const sources = readsFrom(run.current_node_id);
    if (sources.length === 0) {
      waitSources = [];
      waitSourcesLoading = false;
      return;
    }
    waitSourcesLoading = true;
    waitSources = [];
    const read = await Promise.all(
      sources.map(async (nodeId): Promise<WaitContextSource> => {
        try {
          const source = await cockpitApi.getNodeDetail(run.public_run_reference, nodeId);
          return {
            nodeId,
            text: source.answer === null ? null : decodedText(source.answer.value_base64)
          };
        } catch {
          return { nodeId, text: null };
        }
      })
    );
    if (waitContextKey !== key) return;
    waitSources = read;
    waitSourcesLoading = false;
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
      const asked = await cockpitApi.getNodeDetail(
        run.public_run_reference,
        run.current_node_id
      );
      if (asked.job_base64 === null || asked.job_base64.length === 0) {
        waitQuestion = { kind: "absent" };
        return;
      }
      const text = decodedText(asked.job_base64);
      if (text === null) {
        waitQuestion = { kind: "failed", message: "The wait question could not be read." };
        return;
      }
      waitQuestion = text.length === 0 ? { kind: "absent" } : { kind: "present", text };
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

  async function submitWait(typed: string): Promise<void> {
    waitValidationMessage = null;
    waitFailureMessage = null;
    if (typed.trim().length === 0) {
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
        encodeWaitAnswer(typed)
      );
      const prepared = await mutationJournal.prepare(mutation);
      if (prepared.kind !== "wait") throw new Error("The saved request belongs to another operation.");
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
      throw new Error("The workshop confirmed a different answer than the one that was sent.");
    }
    if (result.status === 202 && resolved) {
      throw new Error("Your answer was reported as stored while it is still pending.");
    }
    if (!isRunV3(result.value)) {
      throw new Error("The workshop answered with a run in a format this page cannot read.");
    }
    if (result.value.public_run_reference !== run.public_run_reference) {
      throw new CockpitRequestError("The workshop answered with a different run than this page is showing.");
    }
    onRunRead(result.value);
    if (result.status === 202) {
      const uncertain = await mutationJournal.markUncertain(mutation.mutation_id);
      if (uncertain.kind !== "wait") throw new Error("The accepted request belongs to another operation.");
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
        throw new Error("The document the workshop returned is not the one this run followed.");
      }
      if (revision.graph.workflow_format_version !== 3) {
        throw new Error("This run follows an older document format this page cannot draw.");
      }
      graphRequest = {
        state: "ready",
        name: revision.graph.name,
        description: revision.graph.description,
        previews: revision.graph.node_previews,
        loops: revision.graph.loops,
        waitAnswerSchemas: revision.graph.wait_answer_schemas
      };
    } catch (error) {
      graphRequest = {
        state: "failed",
        message: error instanceof Error ? error.message : "The workflow graph could not be read."
      };
    }
  }

  /**
   * The live stream only speaks when it is *not* healthy.
   *
   * A permanent "Following live" chip is chrome, and a first connect is
   * ordinary loading — neither is worth a line. A stream that has dropped and
   * not come back is different: the operator sat on "Reconnecting" for
   * eighteen minutes with no way out (23.08.). So only that state speaks, and
   * it carries the one act that can fix it. The deeper reconnect semantics —
   * a cursor the other side no longer knows — are #529.
   */
  $: streamSilent =
    projection === null ||
    (projection.protocol_problem === null &&
      projection.connection !== "reconnecting" &&
      projection.connection !== "failed");
</script>

<section class="v3-run" aria-labelledby="v3-run-title">
  <header class="run-head">
    <h1 id="v3-run-title">{headerTitle}</h1>
    {#if description !== null}
      <p class="run-description">{description}</p>
    {/if}
    <p class="run-standing" aria-label="Where this run stands">
      <span class="run-standing-mark run-standing-{standing}" aria-hidden="true">{standingMarks[standing]}</span>
      <strong class="run-standing-word run-standing-{standing}">{wrapDisplayCopy(standingWords[standing])}</strong>
      {#if relativeStanding !== null}<span>{relativeStanding}</span>{/if}
    </p>
    {#if runFactLine !== ""}
      <p class="run-facts">{runFactLine}</p>
    {/if}
  </header>

  {#if stopped !== null}
    <p class="stopped" role="alert"><strong>{stopped[0]}:</strong> {stopped[1]}</p>
  {/if}

  {#if !streamSilent && projection !== null}
    <p class="stream-stale" role="status">
      <span>{wrapDisplayCopy(runPageCopy.streamStale)}</span>
      <button type="button" class="quiet" onclick={onRetryStream}>
        {wrapDisplayCopy(runPageCopy.readAgain)}
      </button>
    </p>
    {#if projection.stream_failure !== null}
      <ProblemNotice problem={projection.stream_failure} />
    {:else if protocolTitle(projection) !== null}
      <ProblemNotice
        title={protocolTitle(projection) ?? "Event invalid"}
        message={protocolDetail(projection) ?? ""}
      />
    {/if}
  {/if}

  {#if waiting}
    {#if waitQuestion.kind === "failed"}
      <ProblemNotice title="The wait question could not be read" message={waitQuestion.message} />
    {/if}
    <V3AnswerCard
      bind:this={answerCard}
      question={waitQuestion.kind === "present" ? waitQuestion.text : null}
      questionMissing={waitQuestion.kind === "absent"}
      questionFailed={waitQuestion.kind === "failed"}
      sources={waitSources}
      sourcesLoading={waitSourcesLoading}
      pending={pendingWait}
      {pendingAnswer}
      accepted={waitAccepted}
      busy={waitBusy}
      validationMessage={waitValidationMessage}
      failureMessage={waitFailureMessage}
      answerKind={currentWaitAnswerSchema?.kind ?? "free"}
      answerValues={currentWaitAnswerSchema?.values ?? []}
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
        <li class="rail-entry">
          <button
            type="button"
            class="node-button"
            aria-expanded={openNodeId === entry.node_id}
            onclick={() => void openNode(entry.node_id)}
          >{entry.node_id}</button>
        </li>
      {/each}
    </ol>
  {:else}
    <WorkflowGraphDrawing
      previews={graphRequest.previews}
      loops={graphRequest.loops}
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
      <NodeDetailPanel
        {detail}
        onClose={closeNode}
        readsFrom={readsFrom(detail.node_id)}
        railAttempt={rail.find((entry) => entry.node_id === detail?.node_id)?.attempt ?? null}
        runEvidence={{
          runId: run.run_id,
          workflowRevisionHash: run.workflow_revision_hash,
          runConfigurationRevisionHash: run.run_configuration_revision_hash,
          terminalHash: run.terminal_hash
        }}
      />
    {:else}
      <p class="muted">Reading {openNodeId}…</p>
    {/if}
  {/if}
</section>

<style>
  .v3-run {
    display: grid;
    gap: var(--space-5);
  }

  .run-head {
    display: grid;
    gap: var(--space-2);
  }

  .run-head h1 {
    margin: 0;
    font-size: clamp(1.6rem, 5vw, 2.4rem);
  }

  .run-description {
    margin: 0;
    max-width: var(--reading-width);
    color: var(--muted);
  }

  .run-standing {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2) var(--space-3);
    margin: 0;
  }

  .run-standing-word {
    font-size: var(--text-md);
  }

  .run-facts {
    margin: 0;
    color: var(--muted);
    font-size: var(--text-xs);
    font-variant-numeric: tabular-nums;
  }

  .run-standing-running {
    color: var(--working);
  }

  .run-standing-waiting {
    color: var(--danger);
  }

  .run-standing-failed {
    color: var(--warning);
  }

  .run-standing-done {
    color: var(--accent);
  }

  .stopped {
    margin: 0;
    padding: var(--space-3) var(--space-4);
    border-left: 4px solid var(--warning);
    border-radius: var(--r);
    background: color-mix(in srgb, var(--warning) 12%, transparent);
    color: var(--warning);
    overflow-wrap: anywhere;
  }

  .stream-stale {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-3);
    margin: 0;
    color: var(--muted);
    font-size: var(--text-sm);
  }

  .rail {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: var(--space-2);
  }

  .node-button {
    display: flex;
    align-items: center;
    width: 100%;
    border: 1px solid var(--line);
    background: var(--panel2);
    font: inherit;
    color: inherit;
    text-align: left;
  }

  .muted {
    color: var(--muted);
  }
</style>
