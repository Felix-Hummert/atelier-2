<script lang="ts">
  import { onMount, tick } from "svelte";

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
  import PoisonedJournalDiscardSheet from "../components/PoisonedJournalDiscardSheet.svelte";
  import ProblemNotice from "../components/ProblemNotice.svelte";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { humanErrorMessage } from "../lib/humanRefusal";
  import { journalPoisonedCopy } from "../lib/journalPoisonedCopy";
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
  import { exactLocal } from "../lib/when";
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

  /**
   * Whether this browser's own memory of pending sendings can be read at all
   * (#914, second half of #1131). The run page reads the same journal
   * `V3RunView.loadPendingWait` and `RunCancelCard`'s two reactive reads --
   * a poisoned journal blocks every one of them, so the whole page stands
   * behind the one honest sentence and its one door instead of letting a
   * child mount and fail silently. `checkJournalHealth` catches the common
   * case (already poisoned before this page opens) before `V3RunView` ever
   * mounts; `onJournalPoisoned`, forwarded through `V3RunView` from its own
   * read and from `RunCancelCard`'s, catches the same journal poisoned in
   * the narrow window between this page's own check and its first read --
   * both paths land on the identical `journalPoisoned` flag.
   */
  let journalPoisoned = false;
  let discardConfirming = false;
  let discardRaw: string | null = null;
  let discardSubmitting = false;
  let discardFailure: string | null = null;
  /** The receipt at display time: gone once this page is left, since no
   * second ledger survives the same poisoned storage (#914 line 12). */
  let discardReceipt: string | null = null;
  let pageRoot: HTMLElement;

  onMount(() => {
    void load();
    void checkJournalHealth();
    return () => {
      disposed = true;
      stream?.close();
      stream = null;
    };
  });

  async function checkJournalHealth(): Promise<void> {
    try {
      await mutationJournal.entries();
    } catch {
      journalPoisoned = true;
    }
  }

  function handleJournalPoisoned(): void {
    journalPoisoned = true;
  }

  function openDiscardConfirm(): void {
    discardRaw = mutationJournal.rawStored();
    discardFailure = null;
    discardConfirming = true;
  }

  function dismissDiscardConfirm(): void {
    discardConfirming = false;
  }

  /**
   * The one door out of a poisoned journal: remove it without ever reading
   * it, then let `V3RunView` remount and read the now-healthy journal fresh
   * -- the same healing `V3RunView`/`RunCancelCard` already do for any other
   * prop change, no page reload (#914 line 3).
   */
  async function confirmDiscardJournal(): Promise<void> {
    discardSubmitting = true;
    discardFailure = null;
    try {
      mutationJournal.discardPoisoned();
      discardReceipt = journalPoisonedCopy.forgottenReceipt(
        exactLocal(new Date().toISOString()),
        new globalThis.TextEncoder().encode(discardRaw ?? "").length
      );
      discardConfirming = false;
      journalPoisoned = false;
      await tick();
      pageRoot?.focus();
    } catch (error) {
      discardFailure = humanErrorMessage(error, journalPoisonedCopy.discardFailure);
    } finally {
      discardSubmitting = false;
    }
  }

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
    // A run whose delivered state already stands still gets no further
    // event: opening the stream anyway only earns the browser's EventSource
    // an ordinary server close it then reconnects from on its own, forever
    // (#1044).
    if (runHasEnded(read.state)) return;
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
    // drops an event still in the page (the 1-vs-2 flake). `runHasEnded` is
    // the one terminality owner, so a live run that lands on CANCELLED closes
    // its stream the same way COMPLETED and FAILED already do.
    if (!runHasEnded(read.state)) return;
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

<section aria-labelledby="v3-run-title" tabindex="-1" bind:this={pageRoot}>
  {#if trail !== null}
    <BackLink label={trail.label} path={trail.path} {navigate} />
  {/if}

  {#if journalPoisoned}
    <!-- Every read this page makes into the journal -- `V3RunView`'s own
         pending wait and `RunCancelCard`'s pending/terminal cancel reads --
         would only be reading this same unreadable memory, so nothing below
         tries: the same one-sentence, one-door brick the Workbench shows
         (mockup v8 `#v8-21-journal-poisoned`). -->
    <ProblemNotice
      title={wrapDisplayCopy(journalPoisonedCopy.sentence)}
      message=""
      actionLabel={wrapDisplayCopy(journalPoisonedCopy.door)}
      onAction={openDiscardConfirm}
    />
  {:else}
    {#if discardReceipt !== null}
      <p class="discard-receipt" role="status">{wrapDisplayCopy(discardReceipt)}</p>
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
        onJournalPoisoned={handleJournalPoisoned}
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
  {/if}

  {#if discardConfirming}
    <PoisonedJournalDiscardSheet
      raw={discardRaw ?? ""}
      submitting={discardSubmitting}
      failure={discardFailure}
      confirmAttributes={{}}
      cancelAttributes={{}}
      onConfirm={() => { void confirmDiscardJournal(); }}
      onDismiss={dismissDiscardConfirm}
    />
  {/if}
</section>

<style>
  .discard-receipt {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-xs);
  }
</style>
