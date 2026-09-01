<script lang="ts">
  import { onMount } from "svelte";

  import {
    CockpitRequestError,
    parseEventCursor,
    type CockpitApi,
    type Problem,
    type RunV3,
    type RunEventSubscription
  } from "../api/client";
  import BackLink from "../components/BackLink.svelte";
  import LoadingState from "../components/LoadingState.svelte";
  import ProblemNotice from "../components/ProblemNotice.svelte";
  import { MutationJournal } from "../lib/mutationJournal";
  import V3RunView from "../components/V3RunView.svelte";
  import {
    beginRead,
    confirmRead,
    failRead,
    retainedRead,
    updateConfirmed,
    type RetainedRead
  } from "../lib/readResource";
  import { inAppRoomOrigin, runBackLink } from "../lib/backLinkCopy";
  import { cockpitRoute } from "../lib/route";
  import { runPageCopy } from "../lib/runPageCopy";
  import { runHasEnded } from "../lib/runState";
  import {
    decodeAndApplyDurableEvent,
    markComplete,
    markConnecting,
    markFailed,
    markLive,
    restartStreamProjection,
    streamProjection,
    type StreamProjection
  } from "../lib/runProjection";

  export let cockpitApi: CockpitApi;
  export let mutationJournal: MutationJournal;
  export let publicReference: string;
  export let navigate: (path: string) => void;
  export let inAppFromPath: string | null = null;

  let snapshot: RetainedRead<RunV3, Problem> = retainedRead<RunV3, Problem>();
  let projection: StreamProjection | null = null;
  let stream: RunEventSubscription | null = null;
  let failureMessage: string | null = null;
  let disposed = false;
  let eventQueue: Promise<void> = Promise.resolve();
  $: run = snapshot.confirmed;
  $: origin = inAppFromPath === null ? null : inAppRoomOrigin(cockpitRoute(inAppFromPath));
  $: trail =
    origin !== null || run !== null
      ? runBackLink(run !== null && runHasEnded(run.state), origin)
      : null;

  onMount(() => {
    void load();
    return () => {
      disposed = true;
      stream?.close();
      stream = null;
    };
  });

  async function load(): Promise<void> {
    const begun = beginRead(snapshot);
    const generation = begun.generation;
    snapshot = begun.read;
    failureMessage = null;
    try {
      const read = await cockpitApi.getRun(publicReference);
      requireRequestedRun(read);
      if (disposed || generation !== snapshot.generation) return;
      snapshot = confirmRead(snapshot, generation, read);
      ensureEventStream(read);
    } catch (error) {
      if (disposed || generation !== snapshot.generation) return;
      if (error instanceof CockpitRequestError && error.problem !== null) {
        snapshot = failRead(snapshot, generation, error.problem);
      } else {
        failureMessage = error instanceof Error ? error.message : runPageCopy.runUnloadable;
        snapshot = { ...snapshot, request: { state: "idle" } };
      }
    }
  }

  function requireRequestedRun(read: RunV3): void {
    if (read.public_run_reference !== publicReference) {
      throw new CockpitRequestError(runPageCopy.differentDurableRun);
    }
  }

  function ensureEventStream(read: RunV3): void {
    if (stream !== null || projection?.connection === "complete" || projection?.connection === "failed") return;
    projection = projection === null
      ? streamProjection(read.public_run_reference, read.workflow_revision_hash)
      : restartStreamProjection(
          projection,
          read.public_run_reference,
          read.workflow_revision_hash
        );
    try {
      stream = cockpitApi.openRunEvents(read.public_run_reference, {
        opened: () => {
          if (projection?.protocol_problem === null && projection.connection !== "failed") {
            projection = markLive(projection);
          }
        },
        event: applyEvent,
        disconnected: () => {
          if (projection !== null && projection.protocol_problem === null && projection.connection !== "complete" && projection.connection !== "failed") {
            projection = markConnecting(projection, true);
          }
        }
      });
    } catch (error) {
      failureMessage = error instanceof Error ? error.message : runPageCopy.eventStreamUnstartable;
      if (projection !== null) projection = markConnecting(projection, true);
    }
  }

  /**
   * Reading the run again, from scratch, whenever the page is not following it.
   *
   * This used to fire only on a protocol violation, on the theory that the
   * browser's own EventSource heals an ordinary drop. It does not always: after
   * a server restart the page sat on "Reconnecting" for eighteen minutes with
   * no way out (operator, 23.08.). So the existing subscription is dropped and
   * both the run and its stream are opened again — an act that is honest in
   * every unhealthy state. What this cannot heal — a cursor the other side no
   * longer knows — is #529.
   */
  function retryStream(): void {
    if (projection === null) return;
    stream?.close();
    stream = null;
    projection = markConnecting(projection);
    void load();
  }

  function applyEvent(rawData: string): void {
    eventQueue = eventQueue.then(() => applyEventInOrder(rawData)).catch((error: unknown) => {
      stream?.close();
      stream = null;
      if (projection !== null) projection = markFailed(projection, null);
      failureMessage = error instanceof Error
        ? error.message
        : runPageCopy.eventUnverified;
    });
  }

  async function applyEventInOrder(rawData: string): Promise<void> {
    if (projection === null) return;
    const priorSequence = projection.last_sequence;
    const next = await decodeAndApplyDurableEvent(projection, rawData);
    projection = next;
    if (next.protocol_problem !== null || next.connection === "failed") {
      stream?.close();
      stream = null;
      return;
    }
    if (next.last_sequence === priorSequence) return;
    // A version 3 line ends on its agent sink, not on a subworkflow node, so
    // the event kind alone cannot say "ended". What can is the run itself: it
    // is re-read once a node has finished its turn, which also carries the
    // rail and the terminal hash the page is showing.
    await refreshWatchedRun(next);
  }

  async function refreshWatchedRun(applied: StreamProjection): Promise<void> {
    let read: RunV3;
    try {
      read = await cockpitApi.getRun(publicReference);
    } catch {
      return;
    }
    if (disposed) return;
    snapshot = updateConfirmed(snapshot, read);
    // The run state only says the cursor will not grow. Completeness is
    // last_sequence matching that cursor. Closing on the terminal state alone
    // drops an event still in the page (the 1-vs-2 flake).
    if (read.state !== "COMPLETED" && read.state !== "FAILED") return;
    if (read.latest_event_cursor === null) return;
    const cursor = parseEventCursor(read.latest_event_cursor);
    if (cursor === null || cursor.publicRunReference !== read.public_run_reference) {
      return;
    }
    if (applied.last_sequence !== cursor.sequence) return;
    projection = markComplete(applied);
    stream?.close();
    stream = null;
  }
</script>

<section aria-labelledby="v3-run-title">
  {#if trail !== null}
    <BackLink label={trail.label} path={trail.path} {navigate} />
  {/if}

  {#if run !== null}
    <V3RunView
      {run}
      {cockpitApi}
      {mutationJournal}
      {projection}
      {navigate}
      onRunRead={(read) => {
        snapshot = updateConfirmed(snapshot, read);
      }}
      onRetryStream={retryStream}
    />
  {:else if snapshot.request.state === "failed"}
    <ProblemNotice problem={snapshot.request.failure} />
  {:else if failureMessage !== null}
    <ProblemNotice title={runPageCopy.runUnavailable} message={failureMessage} />
  {/if}

  {#if run === null}
    {#if snapshot.request.state === "loading"}
      <p class="status"><LoadingState label={runPageCopy.looking} compact /></p>
    {:else}
      <button type="button" onclick={load}>{runPageCopy.retry}</button>
    {/if}
  {/if}
</section>
