<script lang="ts">
  import { onMount, tick } from "svelte";

  import {
    CockpitRequestError,
    type CockpitApi,
    type Run,
    type RunEvent,
    type RunEventSubscription,
    type WorkflowRevisionDetail
  } from "../api/client";
  import HumanActionCard from "../components/HumanActionCard.svelte";
  import NodeRail from "../components/NodeRail.svelte";
  import ProblemNotice from "../components/ProblemNotice.svelte";
  import {
    MutationJournal,
    waitAnswer,
    waitMutation,
    waitMutationId,
    type JournalEntry,
    type WaitMutation
  } from "../lib/mutationJournal";
  import {
    confirmResource,
    decodeAndApplyDurableEvent,
    failResource,
    markComplete,
    markConnecting,
    markLive,
    restartStreamProjection,
    startLoading,
    streamProjection,
    type RetainedResource,
    type StreamProjection
  } from "../lib/runProjection";

  export let cockpitApi: CockpitApi;
  export let mutationJournal: MutationJournal;
  export let publicReference: string;
  export let navigate: (path: string) => void;

  interface RunSnapshot {
    run: Run;
    revision: WorkflowRevisionDetail;
  }

  let snapshot: RetainedResource<RunSnapshot> = {
    confirmed: null,
    request: { state: "idle" }
  };
  let projection: StreamProjection | null = null;
  let stream: RunEventSubscription | null = null;
  let failureMessage: string | null = null;
  let pendingWait: Extract<JournalEntry, { kind: "wait" }> | null = null;
  let waitAccepted = false;
  let waitBusy = false;
  let waitValidationMessage: string | null = null;
  let waitFailureMessage: string | null = null;
  let humanActionCard: HumanActionCard;
  let runStateElement: { focus(): void };
  let loadGeneration = 0;
  let disposed = false;
  $: pendingAnswer = pendingWait === null ? null : waitAnswer(pendingWait);
  $: workingHumanNodeIds = pendingWait === null
    ? new Set<string>()
    : new Set([pendingWait.node_id]);

  onMount(() => {
    void load();
    return () => {
      disposed = true;
      stream?.close();
      stream = null;
    };
  });

  async function load(): Promise<void> {
    const generation = ++loadGeneration;
    snapshot = startLoading(snapshot);
    failureMessage = null;
    try {
      const run = await cockpitApi.getRun(publicReference);
      requireRequestedRun(run);
      const revision =
        snapshot.confirmed?.revision.revision_hash === run.workflow_revision_hash
          ? snapshot.confirmed.revision
          : await cockpitApi.getWorkflowRevision(run.workflow_revision_hash);
      requireBoundRevision(run, revision);
      if (disposed || generation !== loadGeneration) return;
      snapshot = confirmResource(snapshot, { run, revision });
      ensureEventStream(run);
      try {
        await loadPendingWait(run);
      } catch (error) {
        failureMessage = error instanceof Error
          ? error.message
          : "The saved exact answer could not be read.";
      }
      if (disposed || generation !== loadGeneration) return;
    } catch (error) {
      if (disposed || generation !== loadGeneration) return;
      if (error instanceof CockpitRequestError && error.problem !== null) {
        snapshot = failResource(snapshot, error.problem);
      } else {
        failureMessage = error instanceof Error ? error.message : "The durable run could not be loaded.";
        snapshot = { ...snapshot, request: { state: "idle" } };
      }
    }
  }

  function requireRequestedRun(run: Run): void {
    if (run.public_run_reference !== publicReference) {
      throw new CockpitRequestError("The API returned a different durable run.");
    }
  }

  function requireBoundRevision(run: Run, revision: WorkflowRevisionDetail): void {
    const currentNode = revision.graph.nodes.find(
      (node) => node.node_id === run.current_node.node_id
    );
    if (
      revision.revision_hash !== run.workflow_revision_hash ||
      currentNode === undefined ||
      JSON.stringify(currentNode) !== JSON.stringify(run.current_node)
    ) {
      throw new CockpitRequestError("The workflow revision did not match the durable run.");
    }
  }

  function ensureEventStream(run: Run): void {
    if (stream !== null || projection?.connection === "complete") return;
    projection = projection === null
      ? streamProjection(run.public_run_reference, run.workflow_revision_hash)
      : restartStreamProjection(
          projection,
          run.public_run_reference,
          run.workflow_revision_hash
        );
    try {
      stream = cockpitApi.openRunEvents(run.public_run_reference, {
        opened: () => {
          if (projection?.protocol_problem === null) projection = markLive(projection);
        },
        event: applyEvent,
        disconnected: () => {
          if (projection !== null && projection.protocol_problem === null && projection.connection !== "complete") {
            projection = markConnecting(projection, true);
          }
        }
      });
    } catch (error) {
      failureMessage = error instanceof Error ? error.message : "The durable event stream could not start.";
      if (projection !== null) projection = markConnecting(projection, true);
    }
  }

  function applyEvent(rawData: string): void {
    if (projection === null) return;
    const priorSequence = projection.last_sequence;
    const graph = snapshot.confirmed?.revision.graph;
    const next = decodeAndApplyDurableEvent(projection, rawData, graph);
    projection = next;
    if (next.protocol_problem !== null) {
      stream?.close();
      stream = null;
      return;
    }
    if (next.last_sequence === priorSequence) return;
    const latest = next.events.at(-1);
    if (latest?.event === "SUBWORKFLOW_COMPLETED") {
      projection = markComplete(next);
      stream?.close();
      stream = null;
      void followDurableEvent(latest);
    } else if (
      latest?.event === "ACTION_RECONCILIATION_REQUIRED" ||
      latest?.event === "ACTION_RECONCILIATION_RESOLVED" ||
      latest?.event === "WAITING_INPUT" ||
      latest?.event === "WAIT_ANSWERED"
    ) {
      void followDurableEvent(latest);
    }
  }

  async function followDurableEvent(event: RunEvent): Promise<void> {
    let journalFailure: string | null = null;
    try {
      if (event.event === "WAIT_ANSWERED") {
        const mutationId = waitMutationId(event.public_run_reference, event.node_id);
        const entry = await mutationJournal.get(mutationId);
        const resolved = entry?.kind === "wait" && await mutationJournal.resolve(mutationId, {
          type: "wait_answered",
          public_run_reference: event.public_run_reference,
          workflow_revision_hash: event.workflow_revision_hash,
          node_id: event.node_id,
          answer: event.answer,
          answer_hash: event.answer_hash
        });
        if (resolved) {
          pendingWait = null;
          waitAccepted = false;
          waitFailureMessage = null;
        }
      }
    } catch (error) {
      journalFailure = error instanceof Error
        ? error.message
        : "The durable event could not reconcile the saved exact answer.";
    }
    await load();
    if (event.event === "WAIT_ANSWERED") await focusRunState();
    if (journalFailure !== null) failureMessage = journalFailure;
  }

  async function loadPendingWait(run: Run): Promise<void> {
    if (run.state !== "WAITING_INPUT" || run.waiting.type !== "WAITING_INPUT") {
      pendingWait = null;
      waitAccepted = false;
      return;
    }
    const mutationId = waitMutationId(run.public_run_reference, run.waiting.node_id);
    const entry = await mutationJournal.get(mutationId);
    if (entry !== null && entry.kind !== "wait") {
      throw new Error("The saved request identity belongs to another operation.");
    }
    if (entry === null) {
      pendingWait = null;
      waitAccepted = false;
      return;
    }
    if (
      entry.workflow_revision_hash !== run.workflow_revision_hash ||
      entry.node_id !== run.waiting.node_id ||
      entry.public_run_reference !== run.public_run_reference
    ) {
      throw new Error("The saved exact answer does not belong to this waiting node.");
    }
    if (pendingWait?.mutation_id !== entry.mutation_id) waitAccepted = false;
    pendingWait = entry;
  }

  async function submitWait(answer: string): Promise<void> {
    waitValidationMessage = null;
    waitFailureMessage = null;
    if (!/^(?:0|-?[1-9][0-9]*)$/.test(answer)) {
      waitValidationMessage = "Use one canonical integer.";
      return;
    }
    const run = snapshot.confirmed?.run;
    if (run?.state !== "WAITING_INPUT" || run.waiting.type !== "WAITING_INPUT") return;
    waitBusy = true;
    let mutation: WaitMutation | null = null;
    try {
      mutation = await waitMutation(
        run.public_run_reference,
        run.workflow_revision_hash,
        run.waiting.node_id,
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
      waitFailureMessage = error instanceof Error ? error.message : "The answer could not be confirmed.";
    } finally {
      waitBusy = false;
      if (waitFailureMessage !== null) {
        await focusWaitFailure();
      }
    }
  }

  async function retryWait(): Promise<void> {
    if (pendingWait === null) return;
    waitBusy = true;
    waitFailureMessage = null;
    try {
      await deliverWait(pendingWait);
      await focusAfterDelivery();
    } catch (error) {
      await recordWaitFailure(pendingWait.mutation_id, error);
      waitFailureMessage = error instanceof Error ? error.message : "The exact retry could not be confirmed.";
    } finally {
      waitBusy = false;
      if (waitFailureMessage !== null) {
        await focusWaitFailure();
      }
    }
  }

  async function discardWait(): Promise<void> {
    if (pendingWait === null) return;
    await mutationJournal.discard(pendingWait.mutation_id);
    pendingWait = null;
    waitAccepted = false;
    waitValidationMessage = null;
    waitFailureMessage = null;
    await tick();
    humanActionCard?.focusInput();
  }

  async function focusAfterDelivery(): Promise<void> {
    await tick();
    if (pendingWait !== null && waitAccepted) {
      humanActionCard?.focusStatus();
    } else if (pendingWait === null) {
      await focusRunState();
    }
  }

  async function focusWaitFailure(): Promise<void> {
    await tick();
    if (pendingWait !== null) {
      humanActionCard?.focusRetry();
    } else {
      humanActionCard?.focusInput();
    }
  }

  async function focusRunState(): Promise<void> {
    await tick();
    runStateElement?.focus();
  }

  async function deliverWait(mutation: WaitMutation): Promise<void> {
    const result = await cockpitApi.answer(mutation);
    const resolved = await mutationJournal.resolve(mutation.mutation_id, {
      type: "wait_response",
      status: result.status,
      target: mutation.target,
      request_body_base64: mutation.body_base64
    });
    const eventAlreadyProvedAnswer = matchingWaitEventExists(mutation);
    if (result.status === 200 && !resolved && !eventAlreadyProvedAnswer) {
      throw new Error("The answer response did not prove the exact request.");
    }
    if (result.status === 202 && resolved) {
      throw new Error("A pending answer was incorrectly treated as durable completion.");
    }
    requireRequestedRun(result.value);
    const revision = snapshot.confirmed?.revision;
    if (revision === undefined) throw new Error("The bound workflow revision is unavailable.");
    requireBoundRevision(result.value, revision);
    if (eventAlreadyProvedAnswer) {
      pendingWait = null;
      waitAccepted = false;
      await load();
      return;
    }
    snapshot = confirmResource(snapshot, { run: result.value, revision });
    if (result.status === 202) {
      let uncertain: JournalEntry;
      try {
        uncertain = await mutationJournal.markUncertain(mutation.mutation_id);
      } catch (error) {
        if (matchingWaitEventExists(mutation) && await mutationJournal.get(mutation.mutation_id) === null) {
          pendingWait = null;
          waitAccepted = false;
          await load();
          return;
        }
        throw error;
      }
      if (uncertain.kind !== "wait") throw new Error("The accepted request changed kind.");
      pendingWait = uncertain;
      waitAccepted = true;
    } else {
      pendingWait = null;
      waitAccepted = false;
    }
  }

  function matchingWaitEventExists(mutation: WaitMutation): boolean {
    const answer = waitAnswer(mutation);
    return projection?.events.some(
      (event) =>
        event.event === "WAIT_ANSWERED" &&
        event.public_run_reference === mutation.public_run_reference &&
        event.workflow_revision_hash === mutation.workflow_revision_hash &&
        event.node_id === mutation.node_id &&
        event.answer === answer &&
        event.answer_hash === mutation.answer_hash
    ) ?? false;
  }

  async function recordWaitFailure(mutationId: string, error: unknown): Promise<void> {
    if (error instanceof CockpitRequestError && error.definitive_failure) {
      await mutationJournal.discard(mutationId);
      pendingWait = null;
      waitAccepted = false;
      return;
    }
    const entry = await mutationJournal.get(mutationId);
    if (entry?.kind === "wait") {
      const uncertain = await mutationJournal.markUncertain(mutationId);
      if (uncertain.kind === "wait") pendingWait = uncertain;
      waitAccepted = false;
    }
  }

  function connectionLabel(value: StreamProjection): string {
    return {
      connecting: "Connecting",
      live: "Live",
      reconnecting: "Reconnecting",
      complete: "Complete"
    }[value.connection];
  }

  function protocolTitle(value: StreamProjection): string | null {
    if (value.protocol_problem === null) return null;
    return {
      decoder: "Event invalid",
      sequence_gap: "Event gap",
      conflicting_duplicate: "Event conflict"
    }[value.protocol_problem.type];
  }

  function protocolDetail(value: StreamProjection): string | null {
    const problem = value.protocol_problem;
    if (problem === null) return null;
    if (problem.type === "sequence_gap") {
      return `Confirmed sequence ${problem.expected - 1}; received ${problem.received}.`;
    }
    if (problem.type === "conflicting_duplicate") {
      return `Durable cursor ${problem.cursor} was replayed with different bytes.`;
    }
    return "A durable event did not match the closed wire contract.";
  }

  function exactEvent(event: RunEvent): string {
    const bytes = projection?.payload_bytes_by_cursor.get(event.cursor);
    return bytes === undefined ? JSON.stringify(event) : new globalThis.TextDecoder().decode(bytes);
  }
</script>

<section aria-labelledby="run-title">
  <a class="back-link" href="/atelier/runs" onclick={(event) => { event.preventDefault(); navigate("/atelier/runs"); }}>← Runs</a>

  {#if snapshot.request.state === "failed"}
    <ProblemNotice problem={snapshot.request.problem} />
  {:else if failureMessage !== null}
    <ProblemNotice title="Run unavailable" message={failureMessage} />
  {/if}

  {#if snapshot.confirmed !== null}
    <header class="run-header">
      <div>
        <p class="eyebrow">Durable run</p>
        <h1 id="run-title">Run {snapshot.confirmed.run.run_id}</h1>
      </div>
      <button class="quiet" type="button" disabled={snapshot.request.state === "loading"} onclick={load}>Refresh</button>
    </header>

    {#if snapshot.request.state === "loading"}<p class="status compact-status" role="status">Refreshing</p>{/if}

    {#if projection !== null}
      <p class="connection connection-{projection.connection}" role="status">
        <span aria-hidden="true">{projection.connection === "complete" ? "✓" : projection.connection === "live" ? "●" : "↻"}</span>
        {connectionLabel(projection)}
      </p>
      {#if protocolTitle(projection) !== null}
        <ProblemNotice title={protocolTitle(projection) ?? "Event invalid"} message={protocolDetail(projection) ?? ""} />
      {/if}
    {/if}

    <dl class="run-summary">
      <div><dt>State</dt><dd tabindex="-1" bind:this={runStateElement} data-testid="run-state">{snapshot.confirmed.run.state.replaceAll("_", " ").toLowerCase()}</dd></div>
      <div><dt>Workflow</dt><dd><code>{snapshot.confirmed.run.workflow_revision_hash}</code></dd></div>
      {#if snapshot.confirmed.run.terminal_hash !== null}
        <div><dt>Terminal hash</dt><dd><code>{snapshot.confirmed.run.terminal_hash}</code></dd></div>
      {/if}
    </dl>

    {#if snapshot.confirmed.run.state === "WAITING_INPUT" && snapshot.confirmed.run.waiting.type === "WAITING_INPUT"}
      <HumanActionCard
        bind:this={humanActionCard}
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

    <NodeRail
      run={snapshot.confirmed.run}
      graph={snapshot.confirmed.revision.graph}
      events={projection?.events ?? []}
      {workingHumanNodeIds}
    />

    <details class="event-log">
      <summary>Events <span>{projection?.events.length ?? 0}</span></summary>
      {#if (projection?.events.length ?? 0) === 0}
        <p class="empty-event">No durable events yet.</p>
      {:else}
        <ol>
          {#each projection?.events ?? [] as event (event.cursor)}
            <li>
              <span><strong>{event.event.replaceAll("_", " ")}</strong><small>#{event.sequence} · {event.node_id}</small></span>
              <code>{event.event_hash}</code>
              <pre>{exactEvent(event)}</pre>
            </li>
          {/each}
        </ol>
      {/if}
    </details>
  {:else if snapshot.request.state === "loading"}
    <p class="status" role="status">Looking…</p>
  {:else}
    <button type="button" onclick={load}>Retry</button>
  {/if}
</section>
