<script lang="ts">
  import { onMount } from "svelte";

  import {
    CockpitRequestError,
    type CockpitApi,
    type Run,
    type RunEvent,
    type RunEventSubscription,
    type WorkflowRevisionDetail
  } from "../api/client";
  import NodeRail from "../components/NodeRail.svelte";
  import ProblemNotice from "../components/ProblemNotice.svelte";
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
  let loadGeneration = 0;
  let disposed = false;

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
    const graph = snapshot.confirmed?.revision.graph;
    const next = decodeAndApplyDurableEvent(projection, rawData, graph);
    projection = next;
    if (next.protocol_problem !== null) {
      stream?.close();
      stream = null;
      return;
    }
    const latest = next.events.at(-1);
    if (latest?.event === "SUBWORKFLOW_COMPLETED") {
      projection = markComplete(next);
      stream?.close();
      stream = null;
      void load();
    } else if (
      latest?.event === "ACTION_RECONCILIATION_REQUIRED" ||
      latest?.event === "ACTION_RECONCILIATION_RESOLVED" ||
      latest?.event === "WAITING_INPUT" ||
      latest?.event === "WAIT_ANSWERED"
    ) {
      void load();
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
      <div><dt>State</dt><dd>{snapshot.confirmed.run.state.replaceAll("_", " ").toLowerCase()}</dd></div>
      <div><dt>Workflow</dt><dd><code>{snapshot.confirmed.run.workflow_revision_hash}</code></dd></div>
      {#if snapshot.confirmed.run.terminal_hash !== null}
        <div><dt>Terminal hash</dt><dd><code>{snapshot.confirmed.run.terminal_hash}</code></dd></div>
      {/if}
    </dl>

    <NodeRail
      run={snapshot.confirmed.run}
      graph={snapshot.confirmed.revision.graph}
      events={projection?.events ?? []}
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
