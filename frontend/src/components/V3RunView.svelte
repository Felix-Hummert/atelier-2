<script lang="ts">
  import { onMount, tick } from "svelte";

  import {
    type CockpitApi,
    type NodeDetail,
    type RunEvent,
    type RunV3,
    type WorkflowRevisionDetail
  } from "../api/client";
  import { decodeUtf8Base64 } from "../lib/exactBytes";
  import { humanErrorMessage } from "../lib/humanRefusal";
  import {
    MutationJournal,
    waitAnswerText,
    type WaitMutation
  } from "../lib/mutationJournal";
  import { whenFacts, type StreamProjection } from "../lib/runProjection";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { runPageCopy } from "../lib/runPageCopy";
  import { runStanding, standingMarks, standingWords } from "../lib/runState";
  import { protocolDetail, protocolTitle } from "../lib/streamStatus";
  import {
    deliverWaitAnswer,
    loadPendingWaitAnswer,
    prepareWaitAnswer
  } from "../lib/waitAnswerDelivery";
  import { ageLabel } from "../lib/when";
  import NodeDetailPanel from "./NodeDetailPanel.svelte";
  import ProblemNotice from "./ProblemNotice.svelte";
  import ReadableResult from "./ReadableResult.svelte";
  import RunCancelCard from "./RunCancelCard.svelte";
  import V3AnswerCard, { type WaitContextSource } from "./V3AnswerCard.svelte";
  import WorkflowGraphDrawing from "./WorkflowGraphDrawing.svelte";

  /**
   * The run, in the order a person reads it (operator ruling 23.08.):
   *
   * 1. what this is and where it stands — name, description, one plain state
   *    sentence with its duration, and nothing else;
   * 2. what a run that ended wrote — its sink node's declared answer, in one
   *    plain sentence or its fields, never a JSON line (#716). Never for a run
   *    still going: the graph already says where an unfinished line stands.
   * 3. what needs the operator now — the waiting question with the material it
   *    is about, as the one dominant card;
   * 4. the run as a picture — the quiet pipe;
   * 5. everything else only behind a click — node tabs, and every fingerprint
   *    inside the Evidence tab there.
   *
   * An element that fits none of the five does not belong on this page.
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
  let pendingWait: WaitMutation | null = null;
  let waitAccepted = false;
  let waitBusy = false;
  let waitValidationMessage: string | null = null;
  let waitFailureMessage: string | null = null;
  let answerCard: V3AnswerCard;
  $: pendingAnswer = pendingWait === null ? null : waitAnswerText(pendingWait);
  $: waiting = run.state === "WAITING_INPUT";
  $: standing = runStanding(run.state);

  /**
   * What the run's own sink node wrote, read the same way a click into that
   * node would read it (#716) -- the one owner for a node's answer, never a
   * second derivation from the event stream. A run still going or still
   * waiting asks nothing here: `current_node_id` only names the node that
   * closed the line once the run itself has ended.
   */
  let outcomeDetail: NodeDetail | null = null;
  let outcomeKey = "";
  $: runEnded = standing === "done" || standing === "failed" || standing === "cancelled";
  $: if (runEnded) void loadOutcome(run.public_run_reference, run.current_node_id);

  async function loadOutcome(publicRunReference: string, nodeId: string): Promise<void> {
    const key = `${publicRunReference}:${nodeId}`;
    if (key === outcomeKey) return;
    outcomeKey = key;
    let read: NodeDetail | null;
    try {
      read = (await cockpitApi.getNodeDetail(publicRunReference, nodeId)) ?? null;
    } catch {
      read = null;
    }
    if (outcomeKey !== key) return;
    outcomeDetail = read;
  }

  /**
   * The sink's answer, decoded once here (#716) -- a missing answer (a wait
   * node ended the line, or the fetch above failed) and undecodable bytes both
   * read as "nothing to show", which is honest: this banner only ever adds to
   * what the graph and the node panel already prove, never a second, lesser
   * source of the same fact.
   */
  $: outcomeText =
    outcomeDetail === null || outcomeDetail.answer === null
      ? null
      : decodeUtf8Base64(outcomeDetail.answer.value_base64);

  /**
   * The state sentence's relative reading: "for" counts up while the run is
   * still going, "ago" reads the time since it landed -- read off the
   * timestamp each word is actually about (started for "for", ended for
   * "ago"), never the other one, so a run that ended an hour ago cannot read
   * "45 s ago" because that happens to be how long it took to run.
   */
  $: relativeStanding =
    run.ended_at != null
      ? ageLabel(run.ended_at, new Date(), "ago")
      : run.started_at == null
        ? null
        : ageLabel(run.started_at, new Date(), "for");
  /**
   * The exact facts beside the state sentence -- what used to hide behind an
   * "Exact time" reveal link (operator ruling 23.08.: always-visible facts,
   * not a link a person has to find first) and now stands in the same line
   * as the sentence rather than a competing one beneath it (operator ruling
   * 23.08., Zeiten-Hierarchie). A run still going never gets a duration fact:
   * its "for" reading above is already that same elapsed span, and saying it
   * twice is the redundancy the ruling names. A missing timestamp drops its
   * own fact rather than showing a placeholder.
   */
  $: runFacts = whenFacts(run.started_at ?? null, run.ended_at ?? null, new Date());
  $: runFactLine = [
    runFacts.startedExact === null
      ? null
      : `${wrapDisplayCopy(runPageCopy.started)} ${runFacts.startedExact}`,
    runFacts.endedExact === null
      ? null
      : `${wrapDisplayCopy(runPageCopy.ended)} ${runFacts.endedExact}`,
    runFacts.durationWords === null || run.ended_at == null
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
            text: source.answer === null ? null : decodeUtf8Base64(source.answer.value_base64)
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
      const text = decodeUtf8Base64(asked.job_base64);
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
    const lookup = await loadPendingWaitAnswer(
      mutationJournal,
      run.public_run_reference,
      run.workflow_revision_hash,
      run.current_node_id
    );
    if (lookup.kind === "corrupt") {
      waitFailureMessage = lookup.message;
      return;
    }
    if (lookup.kind === "none") {
      pendingWait = null;
      waitAccepted = false;
      return;
    }
    pendingWait = lookup.pending;
    waitAccepted = false;
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
    try {
      const mutation = await prepareWaitAnswer(
        mutationJournal,
        run.public_run_reference,
        run.workflow_revision_hash,
        run.current_node_id,
        typed
      );
      pendingWait = mutation;
      waitAccepted = false;
      await deliverAndSettle(mutation, "The answer could not be confirmed.");
      await focusAfterDelivery();
    } catch (error) {
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
      await deliverAndSettle(pendingWait, "The exact retry could not be confirmed.");
      await focusAfterDelivery();
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

  /** The one audited delivery path (#572): the run page and the Board send through the same function. */
  async function deliverAndSettle(mutation: WaitMutation, fallbackMessage: string): Promise<void> {
    const outcome = await deliverWaitAnswer(cockpitApi, mutationJournal, mutation, fallbackMessage);
    if (outcome.kind === "confirmed") {
      onRunRead(outcome.run);
      pendingWait = null;
      waitAccepted = false;
      return;
    }
    if (outcome.kind === "uncertain") {
      onRunRead(outcome.run);
      pendingWait = outcome.pending;
      waitAccepted = true;
      return;
    }
    pendingWait = outcome.pending;
    waitAccepted = false;
    waitFailureMessage = outcome.message;
    await focusWaitFailure();
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
      {#if runFactLine !== ""}<span class="run-facts">· {runFactLine}</span>{/if}
    </p>
  </header>

  {#if stopped !== null}
    <p class="stopped" role="alert"><strong>{stopped[0]}:</strong> {stopped[1]}</p>
  {/if}

  {#if outcomeText !== null}
    <section class="run-outcome card" aria-label={wrapDisplayCopy(runPageCopy.tabResult)}>
      <ReadableResult decodedAnswer={outcomeText} />
    </section>
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

  <!-- Work first, brake second (HEART "The place"): the run's own shapes lead,
       and the cancel sits below them. It lifts itself back to the top only while
       a cancel is actually in flight, when it is the room's news. -->
  <RunCancelCard {run} {cockpitApi} {mutationJournal} {onRunRead} />

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

  /* The run's identity always leads the room, so it sits ahead of an in-flight
     cancel card even when that card lifts itself toward the top. */
  .run-head {
    display: grid;
    gap: var(--space-2);
    order: -2;
  }

  .run-head h1 {
    margin: 0;
  }

  /* Dimmed and italic reads as prose about the run, not a system line, so a
     reader never has to guess what kind of sentence this is (operator ruling
     23.08.). */
  .run-description {
    margin: 0;
    max-width: var(--reading-width);
    color: var(--ink-dim);
    font-style: italic;
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

  /* The exact facts read as a quiet clause of the same sentence, not a second
     one -- one hierarchy, not two competing lines (operator ruling 23.08.). */
  .run-facts {
    color: var(--ink-dim);
    font-size: var(--text-xs);
    font-variant-numeric: tabular-nums;
  }

  .run-standing-running {
    color: var(--signal-live);
  }

  .run-standing-waiting {
    color: var(--signal-attention);
  }

  .run-standing-failed {
    color: var(--signal-failure);
  }

  .run-standing-done {
    color: var(--signal-quiet);
  }

  .stopped {
    margin: 0;
    padding: var(--space-3) var(--space-4);
    border-left: var(--edge-mark) solid var(--signal-failure);
    border-radius: var(--r);
    background: color-mix(in srgb, var(--signal-failure) var(--wash), var(--panel2));
    color: var(--signal-failure);
    overflow-wrap: anywhere;
  }

  .stream-stale {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-3);
    margin: 0;
    color: var(--ink-dim);
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
    border: var(--edge) solid var(--line);
    background: var(--panel2);
    font: inherit;
    color: inherit;
    text-align: left;
  }

  .muted {
    color: var(--ink-dim);
  }
</style>
