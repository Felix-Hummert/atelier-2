<script lang="ts">
  import { onMount } from "svelte";

  import {
    isRunV3,
    type AnyRun,
    type CockpitApi,
    type RunEvent,
    type RunEventSubscription
  } from "../api/client";
  import InboxRow from "../components/InboxRow.svelte";
  import ProblemNotice from "../components/ProblemNotice.svelte";
  import ProjectCard from "../components/ProjectCard.svelte";
  import {
    applyAttentionFrame,
    attentionStopped,
    markAttentionConnecting,
    markAttentionLive,
    startAttentionHold,
    type AttentionHold
  } from "../lib/attentionHold";
  import { humanErrorMessage } from "../lib/humanRefusal";
  import { readEveryRun } from "../lib/runPages";
  import { confirmResource, startLoading, type RetainedResource } from "../lib/runProjection";
  import { countStanding, runsStanding } from "../lib/runState";
  import {
    connectionLabel,
    protocolDetail,
    protocolTitle,
    streamStopped
  } from "../lib/streamStatus";

  export let cockpitApi: CockpitApi;
  export let navigate: (path: string) => void;

  type StudioHome = {
    runs: AnyRun[];
  };

  let home: RetainedResource<StudioHome> = { confirmed: null, request: { state: "idle" } };
  let hold: AttentionHold = startAttentionHold();
  let stream: RunEventSubscription | null = null;
  let failureMessage: string | null = null;
  let projectionFailure: string | null = null;
  let disposed = false;
  let eventQueue: Promise<void> = Promise.resolve();
  const pendingEvents: RunEvent[] = [];

  onMount(() => {
    void load();
    holdAttention();
    return () => {
      disposed = true;
      stream?.close();
      stream = null;
    };
  });

  async function load(): Promise<void> {
    home = startLoading(home);
    failureMessage = null;
    try {
      const [started, waitingInput, waitingReconciliation, completed, failed] = await Promise.all([
        readEveryRun((after) => cockpitApi.listRuns(after, "STARTED")),
        readEveryRun((after) => cockpitApi.listRuns(after, "WAITING_INPUT")),
        readEveryRun((after) => cockpitApi.listRuns(after, "WAITING_RECONCILIATION")),
        readEveryRun((after) => cockpitApi.listRuns(after, "COMPLETED")),
        readEveryRun((after) => cockpitApi.listRuns(after, "FAILED"))
      ]);
      const unread = [started, waitingInput, waitingReconciliation, completed, failed]
        .filter((reading) => !reading.complete)
        .map((reading) => ("unreadable" in reading ? reading.unreadable : ""))
        .filter((text) => text.length > 0);
      upsertRuns([
        ...started.runs,
        ...waitingInput.runs,
        ...waitingReconciliation.runs,
        ...completed.runs,
        ...failed.runs
      ]);
      if (unread.length > 0) {
        failureMessage = `Some of this workshop could not be read, so what is below is incomplete: ${unread.join("; ")}.`;
      }
    } catch (error) {
      failureMessage = error instanceof Error ? error.message : "The workshop could not be read.";
      home = { ...home, request: { state: "idle" } };
    }
  }

  function upsertRuns(runs: readonly AnyRun[]): void {
    const known = [...(home.confirmed?.runs ?? [])];
    for (const run of runs) {
      const index = known.findIndex(
        (item) => item.public_run_reference === run.public_run_reference
      );
      if (index >= 0) {
        const current = known[index];
        if (current !== undefined && run.state_version < current.state_version) continue;
        known[index] = run;
      } else {
        known.push(run);
      }
    }
    home = confirmResource(home, { runs: known });
  }

  function holdAttention(): void {
    if (stream !== null || attentionStopped(hold)) return;
    try {
      stream = cockpitApi.openAttentionEvents({
        opened: () => {
          hold = markAttentionLive(hold);
        },
        event: applyEvent,
        disconnected: () => {
          hold = markAttentionConnecting(hold, true);
        }
      });
    } catch (error) {
      hold = markAttentionConnecting(hold, true);
      failureMessage =
        error instanceof Error ? error.message : "The attention stream could not start.";
    }
  }

  function applyEvent(rawData: string): void {
    const applied = applyAttentionFrame(hold, rawData);
    hold = applied.hold;
    if (applied.event === null) {
      if (attentionStopped(hold)) {
        stream?.close();
        stream = null;
      }
      return;
    }
    pendingEvents.push(applied.event);
    queueDrain();
  }

  function retryProjection(): void {
    if (disposed) return;
    projectionFailure = null;
    queueDrain();
  }

  function queueDrain(): void {
    eventQueue = eventQueue.then(drainAttention).catch((error: unknown) => {
      if (disposed) return;
      projectionFailure = humanErrorMessage(
        error,
        "The attention event could not be applied."
      );
    });
  }

  async function drainAttention(): Promise<void> {
    if (disposed || projectionFailure !== null) return;
    while (!disposed && pendingEvents.length > 0 && projectionFailure === null) {
      const event = pendingEvents[0];
      if (event === undefined) break;
      try {
        const run = await cockpitApi.getRun(event.public_run_reference);
        if (disposed) return;
        upsertRuns([run]);
        pendingEvents.shift();
      } catch (error) {
        if (disposed) return;
        projectionFailure = humanErrorMessage(
          error,
          "The attention event could not be applied."
        );
        return;
      }
    }
  }

  function lastLanding(runs: readonly AnyRun[]): string | null {
    const stamps = runs
      .filter(isRunV3)
      .map((run) => run.ended_at)
      .filter((value): value is string => value !== null)
      .sort();
    return stamps.at(-1) ?? null;
  }

  $: snapshot = home.confirmed;
  $: runs = snapshot?.runs ?? [];
  $: runningCount = countStanding(runs, "running");
  $: waitingRuns = runsStanding(runs, "waiting");
  $: failedCount = countStanding(runs, "failed");
  $: landedCount = countStanding(runs, "done");
  $: lastLandedAt = lastLanding(runsStanding(runs, "done"));
  $: empty =
    snapshot !== null &&
    runningCount === 0 &&
    waitingRuns.length === 0 &&
    failedCount === 0 &&
    landedCount === 0 &&
    hold.connection === "live" &&
    !streamStopped(hold);
  $: canStart = !attentionStopped(hold) && projectionFailure === null;
</script>

<section class="studio-home" aria-labelledby="studio-title">
  <section class="studio-chat" aria-labelledby="chat-title">
    <div class="studio-chat-head">
      <h2 id="chat-title">Chat</h2>
      <p>about everything — when the conductor exists</p>
    </div>
    <p>
      The conductor is not built yet
      (<a href="https://github.com/FlexOr2/atelier-2/issues/7">#7</a>).
      When it is, it answers here — and the same door stands on the project and on the run.
    </p>
  </section>

  <div class="studio-board">
    <header class="page-header">
      <div>
        <p class="eyebrow">Atelier</p>
        <h1 id="studio-title">Studio</h1>
      </div>
      {#if snapshot !== null && !empty && canStart}
        <a class="button primary" href="/atelier/new" onclick={(event) => { event.preventDefault(); navigate("/atelier/new"); }}>Start</a>
      {/if}
    </header>

    <p
      class="connection connection-{hold.connection}"
      class:connection-problem={streamStopped(hold)}
      role="status"
    >
      <span aria-hidden="true">{streamStopped(hold) ? "◇" : hold.connection === "live" ? "●" : "↻"}</span>
      {connectionLabel(hold)}
    </p>
    {#if hold.stream_failure !== null}
      <ProblemNotice problem={hold.stream_failure} />
    {:else if protocolTitle(hold) !== null}
      <ProblemNotice title={protocolTitle(hold) ?? "Event invalid"} message={protocolDetail(hold) ?? ""} />
    {/if}

    {#if projectionFailure !== null}
      <ProblemNotice message={projectionFailure} />
      <button type="button" onclick={retryProjection}>Retry</button>
    {/if}
    {#if failureMessage !== null}
      <ProblemNotice message={failureMessage} />
    {/if}

    {#if snapshot !== null}
      <InboxRow runs={waitingRuns} {navigate} />

      {#if empty}
        <div class="empty">
          <h2>Nothing is running</h2>
          <p>A workflow becomes a run, and a run is what this workshop shows.</p>
          {#if canStart}
            <a class="button primary" href="/atelier/new" onclick={(event) => { event.preventDefault(); navigate("/atelier/new"); }}>Start a run</a>
          {/if}
        </div>
      {:else if runningCount > 0 || waitingRuns.length > 0 || failedCount > 0 || landedCount > 0}
        <h2 class="section-title">Projects</h2>
        <ProjectCard
          running={runningCount}
          waiting={waitingRuns.length}
          failed={failedCount}
          landed={landedCount}
          lastLandedAt={lastLandedAt}
          {navigate}
        />
      {/if}
    {:else if home.request.state === "loading"}
      <p class="status" role="status">Looking…</p>
    {/if}
  </div>
</section>
