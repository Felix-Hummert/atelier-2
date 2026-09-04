<script lang="ts">
  import { onMount, tick } from "svelte";

  import {
    type CockpitApi,
    type DefectiveRunRow,
    type RunEvent,
    type RunEventSubscription,
    type RunV3
  } from "../api/client";
  import DefectiveRunRowItem from "../components/DefectiveRunRow.svelte";
  import PinnedDecision from "../components/PinnedDecision.svelte";
  import PoisonedJournalDoor from "../components/PoisonedJournalDoor.svelte";
  import ProblemNotice from "../components/ProblemNotice.svelte";
  import ReadState from "../components/ReadState.svelte";
  import {
    applyAttentionFrame,
    attentionStopped,
    markAttentionConnecting,
    markAttentionLive,
    startAttentionHold,
    type AttentionHold
  } from "../lib/attentionHold";
  import {
    currentChatTranscript,
    sendChatTurn,
    subscribeChatTranscript,
    type ChatMessage
  } from "../lib/chatTranscript";
  import {
    answerConductorWait,
    conductorConversationCopy,
    decodeConductorEvent,
    emptyConductorTranscript,
    rememberConductorRun,
    rememberedConductorRun,
    reduceConductorEvent,
    startConductorConversation,
    type ConductorMessage,
    type ConductorTranscript
  } from "../lib/conductorConversation";
  import { conductorChatCopy } from "../lib/conductorChatCopy";
  import {
    conductorConversationShape,
    newestConductorConversation,
    resolveConductorConnection,
    type ConductorConnectionState
  } from "../lib/conductorEpisode";
  import { connectionState, onConnectionRecovered, restartNoticeCopy } from "../lib/connectionState";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { humanErrorMessage } from "../lib/humanRefusal";
  import type { MutationJournal } from "../lib/mutationJournal";
  import {
    beginRead,
    confirmRead,
    failRead,
    retainedRead,
    updateConfirmed,
    type RetainedRead
  } from "../lib/readResource";
  import { runPageCopy } from "../lib/runPageCopy";
  import { runPath } from "../lib/route";
  import { newestReadOfEachRun, resolveWorkflowName, splitRunListRows } from "../lib/runList";
  import { readEveryRevision, readEveryRun } from "../lib/runPages";
  import { loadPendingWaitAnswer, type PendingWaitLookup } from "../lib/waitAnswerDelivery";
  import { humanMove, runHasEnded, runStanding, standingMarks } from "../lib/runState";
  import {
    connectionLabel,
    protocolDetail,
    protocolTitle,
    streamStopped
  } from "../lib/streamStatus";
  import { ageLabel } from "../lib/when";
  import { absorbAttentionRun, workbenchDecisionPins } from "../lib/workbenchAttention";
  import { workbenchPageCopy } from "../lib/workbenchPageCopy";
  import { workbenchQuestionAttribute, workbenchQuestions } from "../lib/workbenchQuestions";
  import { WORKSHOP_DESTINATION, runsWaitingForYou } from "../lib/workshop";

  /**
   * The Workbench: what wants you now, what is moving, what was said, and the
   * ear (ADR 0019 §1).
   *
   * A decision stands pinned in its own non-scrolling region until it is
   * answered -- the whole point of issue #580, because a decision request once
   * got lost in the growing stream. Beneath it lies the living shelf: the runs
   * that are moving, each one click from its graph. The Board that used to hold
   * those runs is gone, and nothing shows them twice.
   *
   * The room is alive: it holds the attention stream the Board used to hold, so
   * a decision that opens while the operator is sitting here appears where it
   * belongs instead of waiting for the next visit. An event is only a nudge --
   * every one of them is projected through the canonical `getRun` read, so what
   * this room shows is always a run the API confirmed, never a frame's own
   * story.
   */
  export let cockpitApi: CockpitApi;
  export let mutationJournal: MutationJournal;
  export let navigate: (path: string) => void;

  type WorkbenchRuns = {
    runs: RunV3[];
    /** Runs whose own projection failed (#1042): read apart, shown apart. */
    defective: DefectiveRunRow[];
    /** Null when the described catalog could not be read this round: enrichment, not a gate. */
    workflowNames: ReadonlyMap<string, string | null> | null;
  };

  type ReadFailure =
    | { kind: "unavailable"; title: string }
    | { kind: "incomplete"; title: string };

  /**
   * Whether a conductor reads this ear. "reading" is the moment before the
   * answer is known and "unreadable" the honest state when the reads themselves
   * failed -- neither pretends any of `ConductorConnectionState`'s own answers,
   * because either would be a guess dressed as a fact.
   */
  type ConductorLink = { kind: "reading" } | { kind: "unreadable" } | ConductorConnectionState;

  let live: RetainedRead<WorkbenchRuns, ReadFailure> = retainedRead<WorkbenchRuns, ReadFailure>();
  let hold: AttentionHold = startAttentionHold();
  let stream: RunEventSubscription | null = null;
  let streamFailureMessage: string | null = null;
  let projectionFailure: string | null = null;
  let disposed = false;
  let eventQueue: Promise<void> = Promise.resolve();
  const pendingEvents: RunEvent[] = [];
  let transcript: readonly ChatMessage[] = currentChatTranscript();
  let conversationTranscript: readonly (ChatMessage | ConductorMessage)[] = transcript;
  let conductorTranscript: ConductorTranscript = emptyConductorTranscript();
  let conductorRun: RunV3 | null = null;
  let conductorStream: RunEventSubscription | null = null;
  let conductorStreamReference: string | null = null;
  let firstConversationMessage: string | null = null;
  let conductorDeliveryBusy = false;
  let conductorDeliveryFailure: string | null = null;
  let typed = "";
  let expandedPinReference: string | null = null;
  let composer: { focus(): void };
  let conductorLink: ConductorLink = { kind: "reading" };

  /**
   * Whether this browser's own memory of pending sendings can be read at all
   * (#914). A poisoned journal blocks every read that would otherwise show a
   * pinned decision or the conductor's own pending wait, so the whole room
   * stands behind `PoisonedJournalDoor`'s one honest sentence and its one
   * door instead of quietly never showing the cards that would have read it.
   */
  let journalPoisoned = false;
  let roomHeading: { focus(): void };

  const speakerLabels: Record<ChatMessage["speaker"], string> = {
    you: workbenchPageCopy.youLabel,
    house: workbenchPageCopy.houseLabel
  };

  const catalogPath = WORKSHOP_DESTINATION.catalog.path;
  const settingsPath = WORKSHOP_DESTINATION.settings.path;

  onMount(() => {
    void load();
    holdAttention();
    void resolveConductor();
    const unsubscribe = subscribeChatTranscript((next) => {
      transcript = next;
    });
    // A read that failed while the connection was lost stays failed once the
    // connection returns until something asks again -- reload was the only
    // way out (#700).
    const unsubscribeConnection = onConnectionRecovered(() => {
      void load();
      void resolveConductor();
    });
    return () => {
      disposed = true;
      stream?.close();
      stream = null;
      conductorStream?.close();
      conductorStream = null;
      unsubscribe();
      unsubscribeConnection();
    };
  });

  async function resolveConductor(): Promise<void> {
    try {
      const state = await resolveConductorConnection(cockpitApi);
      conductorLink = state;
      if (state.kind === "connected" && live.confirmed !== null) {
        void selectConductorConversation(live.confirmed.runs);
      }
      if (state.kind === "connected") void restoreConductorConversation();
    } catch {
      conductorLink = { kind: "unreadable" };
    }
  }

  /**
   * Re-reads the conductor's own pending wait once the journal has just
   * healed (#914 second half, #1131): un-hiding the pinned-decision region
   * already lets each pin mount and read the now-healthy journal on its
   * own; the conductor's own pending wait needs this one explicit nudge,
   * since nothing else prompts it right now.
   */
  function healJournal(): void {
    if (conductorRun !== null) void refreshConductorRun(conductorRun.public_run_reference);
  }

  function followConductor(run: RunV3): void {
    if (conductorStreamReference === run.public_run_reference) return;
    conductorStream?.close();
    conductorStream = null;
    conductorStreamReference = run.public_run_reference;
    rememberConductorRun(sessionStorage, run.public_run_reference);
    conductorTranscript = emptyConductorTranscript();
    conductorStream = cockpitApi.openRunEvents(run.public_run_reference, {
      opened: () => {},
      event: (rawData) => {
        const event = decodeConductorEvent(rawData);
        if (event === null) return;
        conductorTranscript = reduceConductorEvent(conductorTranscript, event);
        void refreshConductorRun(run.public_run_reference);
      },
      disconnected: () => {}
    });
  }

  async function restoreConductorConversation(): Promise<void> {
    const publicRunReference = rememberedConductorRun(sessionStorage);
    if (publicRunReference === null || conductorRun !== null) return;
    try {
      const run = await cockpitApi.getRun(publicRunReference);
      const revision = await cockpitApi.getWorkflowRevision(run.workflow_revision_hash);
      if (conductorConversationShape(revision) === null || conductorRun !== null) return;
      conductorRun = run;
      followConductor(run);
      await refreshConductorRun(run.public_run_reference);
    } catch {
      // A stale local reference is not a conversation. The normal live-run
      // selection remains the only fallback instead of inventing one.
    }
  }

  async function selectConductorConversation(runs: readonly RunV3[]): Promise<void> {
    const revisions = await Promise.all(
      [...new Set(runs.map((run) => run.workflow_revision_hash))].map(async (workflowRevisionHash) => {
        try {
          const revision = await cockpitApi.getWorkflowRevision(workflowRevisionHash);
          return conductorConversationShape(revision) === null ? null : workflowRevisionHash;
        } catch {
          return null;
        }
      })
    );
    const conductorRevisions = new Set(
      revisions.filter((workflowRevisionHash): workflowRevisionHash is string => workflowRevisionHash !== null)
    );
    const selected = newestConductorConversation(runs, conductorRevisions);
    if (selected === null || selected.public_run_reference === conductorRun?.public_run_reference) return;
    conductorRun = selected;
    followConductor(selected);
    void refreshConductorRun(selected.public_run_reference);
  }

  /**
   * The conductor's own pending-wait read, kept apart from `refreshConductorRun`'s
   * outer catch so a poisoned journal is told apart from an ordinary network
   * failure -- the outer catch's "retry next frame" comment is honest only for
   * the latter (#914). A poisoned journal instead raises the room's one shared
   * notice, the same one a pinned decision's own read would raise if it ran.
   */
  async function readPendingConductorWait(run: RunV3): Promise<PendingWaitLookup | null> {
    try {
      return await loadPendingWaitAnswer(
        mutationJournal,
        run.public_run_reference,
        run.workflow_revision_hash,
        run.current_node_id,
        run.current_node_execution_id
      );
    } catch {
      journalPoisoned = true;
      return null;
    }
  }

  async function refreshConductorRun(publicRunReference: string): Promise<void> {
    try {
      const refreshed = await cockpitApi.getRun(publicRunReference);
      if (conductorRun?.public_run_reference !== publicRunReference) return;
      conductorRun = refreshed;
      if (firstConversationMessage !== null && refreshed.state === "WAITING_INPUT") {
        const firstMessage = firstConversationMessage;
        firstConversationMessage = null;
        await deliverConductorMessage(refreshed, firstMessage);
        return;
      }
      if (refreshed.state === "WAITING_INPUT") {
        const pending = await readPendingConductorWait(refreshed);
        if (pending === null) return;
        conductorDeliveryBusy = pending.kind === "found";
        if (pending.kind === "corrupt") conductorDeliveryFailure = pending.message;
      } else {
        conductorDeliveryBusy = refreshed.state === "STARTED" || refreshed.state === "WAITING_RECONCILIATION";
      }
    } catch {
      // The stream remains the durable transcript. The next frame retries the
      // canonical run read without replacing it with a guess.
    }
  }

  /**
   * Every run that still moves or waits for a person, read fresh on each visit.
   *
   * The three non-terminal run states are one logical read: any of them
   * incomplete stops the confirm, because a part shown as the whole would hide
   * a run that wants you. A terminal run belongs to History and is never asked
   * for here. The catalog read is enrichment over that truth, never a gate on
   * it -- a run still confirms with its own real fields when its name could not
   * be resolved, falling back to the run id honestly.
   *
   * The list is the cold read; the attention hold nudges each change through
   * the same canonical `getRun` so a decision that opens while the operator
   * is sitting here appears without a reload.
   */
  async function load(): Promise<void> {
    const begun = beginRead(live);
    live = begun.read;
    try {
      const [started, waitingInput, waitingReconciliation, revisions] = await Promise.all([
        readEveryRun((after) => cockpitApi.listRuns(after, "STARTED")),
        readEveryRun((after) => cockpitApi.listRuns(after, "WAITING_INPUT")),
        readEveryRun((after) => cockpitApi.listRuns(after, "WAITING_RECONCILIATION")),
        readEveryRevision((after) => cockpitApi.listWorkflowRevisions(after))
      ]);
      const runReadings = [started, waitingInput, waitingReconciliation];
      if (runReadings.some((reading) => !reading.complete)) {
        live = failRead(live, begun.generation, {
          kind: "incomplete",
          title: wrapDisplayCopy(workbenchPageCopy.runsIncomplete)
        });
        return;
      }
      const workflowNames = revisions.complete
        ? new Map(
            revisions.revisions.map((revision) => [revision.workflow_revision_hash, revision.name])
          )
        : null;
      const rows = newestReadOfEachRun(runReadings.flatMap((reading) => reading.runs));
      const { runs, defective } = splitRunListRows(rows);
      confirm(begun.generation, { runs, defective, workflowNames });
    } catch {
      live = failRead(live, begun.generation, {
        kind: "unavailable",
        title: wrapDisplayCopy(workbenchPageCopy.runsUnavailable)
      });
    }
  }

  function confirm(generation: number, confirmed: WorkbenchRuns): void {
    const before = live;
    live = confirmRead(live, generation, confirmed);
    if (live === before) return;
    publishCount(confirmed.runs);
    if (conductorLink.kind === "connected") {
      void selectConductorConversation(confirmed.runs);
    }
    // An event that arrived while this read was in flight has truth to be
    // absorbed into now.
    if (pendingEvents.length > 0) queueDrain();
  }

  /**
   * The hold of the attention stream: the one door through which this room
   * learns that something changed while the operator is looking at it.
   */
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
      streamFailureMessage = humanErrorMessage(
        error,
        wrapDisplayCopy(workbenchPageCopy.streamUnstartable)
      );
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
        wrapDisplayCopy(workbenchPageCopy.eventUnapplied)
      );
    });
  }

  /**
   * Each nudged run, read canonically and absorbed in the order the events
   * arrived. A read that fails keeps its event at the head of the queue: the
   * room says so and offers one move rather than skipping a run and pretending
   * it is up to date.
   */
  async function drainAttention(): Promise<void> {
    if (disposed || projectionFailure !== null) return;
    while (!disposed && pendingEvents.length > 0 && projectionFailure === null) {
      const event = pendingEvents[0];
      if (event === undefined) break;
      try {
        const run = await cockpitApi.getRun(event.public_run_reference);
        if (disposed) return;
        // Nothing to absorb it into yet: the event waits for the first
        // confirmed read rather than being dropped or inventing a room.
        if (!absorbRun(run)) return;
        pendingEvents.shift();
      } catch (error) {
        if (disposed) return;
        projectionFailure = humanErrorMessage(
          error,
          wrapDisplayCopy(workbenchPageCopy.eventUnapplied)
        );
        return;
      }
    }
  }

  /** The rail's ochre count: the last confirmed read, and no earlier. */
  function publishCount(runs: readonly RunV3[]): void {
    runsWaitingForYou.set(runs.filter((run) => runStanding(run.state) === "waiting").length);
  }

  /**
   * One canonically read run, taken into what this room shows -- whether it
   * came from an answered decision or from the attention stream.
   *
   * A run that still moves or waits stands here with its fresher truth; one
   * that has ended leaves for History the moment it does, so a pin never
   * lingers as a question the run no longer asks and the shelf never keeps a
   * finished row.
   *
   * Nothing is absorbed while this room holds no confirmed truth of its own --
   * inventing a one-run list out of a stream would dress a pending or failed
   * read up as a room. The caller keeps the event instead, so it lands the
   * moment a read confirms.
   */
  // Returns whether the read could be taken in; see the note above.
  function absorbRun(read: RunV3): boolean {
    const confirmed = live.confirmed;
    if (confirmed === null) return false;
    const runs = absorbAttentionRun(confirmed.runs, read);
    live = updateConfirmed(live, { ...confirmed, runs });
    publishCount(runs);
    return true;
  }

  /**
   * A connected conductor starts one loop run for the first message, then
   * turns every later message into its current wait answer. "unreadable"
   * keeps the standing honest local refusal, where nothing was started is
   * still the whole truth. "absent", "unbound", "not-startable" and
   * "reading" (#1103, #1114) are a different kind of state: a real reason —
   * or, for "reading", a real unknown still in flight — the composer is
   * locked for, so nothing is sent at all rather than accepted and
   * swallowed. A message sent while "reading" would otherwise fall into the
   * "unreadable"/local-chat branch below and be answered locally, and the
   * moment the read resolves to "connected" that locally-answered turn has
   * nothing to do with the real conversation the operator meant to start.
   *
   * A lost connection (#700), or one of those four locked states, keeps the
   * message in the box instead: the send button is disabled the same moment,
   * so this guard only catches the keyboard's Enter shortcut racing that
   * disable.
   */
  async function send(event: Event): Promise<void> {
    event.preventDefault();
    const message = typed.trim();
    if (message.length === 0 || $connectionState === "reconnecting" || composerLocked) return;
    if (conductorLink.kind === "connected") {
      if (
        conductorDeliveryBusy ||
        (conductorRun !== null &&
          conductorRun.state !== "WAITING_INPUT" &&
          !runHasEnded(conductorRun.state))
      ) {
        return;
      }
      conductorDeliveryFailure = null;
      typed = "";
      if (conductorRun?.state === "WAITING_INPUT") {
        await deliverConductorMessage(conductorRun, message);
      } else {
        conductorDeliveryBusy = true;
        firstConversationMessage = message;
        try {
          conductorRun = await startConductorConversation(cockpitApi, conductorLink.connection);
          followConductor(conductorRun);
          await refreshConductorRun(conductorRun.public_run_reference);
        } catch (error) {
          conductorDeliveryBusy = false;
          firstConversationMessage = null;
          typed = message;
          conductorDeliveryFailure = humanErrorMessage(error, conductorChatCopy.startRefused);
        }
      }
    } else {
      sendChatTurn(typed);
      transcript = currentChatTranscript();
      typed = "";
    }
    await tick();
    composer.focus();
  }

  async function deliverConductorMessage(run: RunV3, message: string): Promise<void> {
    conductorDeliveryBusy = true;
    try {
      const outcome = await answerConductorWait(cockpitApi, mutationJournal, run, message);
      if (outcome.kind === "failed") {
        typed = message;
        conductorDeliveryFailure = outcome.message;
        return;
      }
      conductorRun = outcome.run;
    } catch (error) {
      // A retry of an already-open wait with edited text can conflict with
      // its own earlier, differently-worded attempt still in the journal
      // (mutationJournal.ts) -- the composer unlocks and keeps the words
      // instead of leaving the room silently stuck (#959).
      typed = message;
      conductorDeliveryFailure = humanErrorMessage(error, runPageCopy.answerUnconfirmed);
    } finally {
      conductorDeliveryBusy = false;
    }
  }

  /**
   * Enter sends, Shift+Enter keeps writing -- the shape every composer has, and
   * the reason the field is a textarea: a message to the house is often more
   * than one line.
   */
  function keydown(event: KeyboardEvent): void {
    if (event.key !== "Enter" || event.shiftKey) return;
    void send(event);
  }

  $: streamTitle = protocolTitle(hold);
  $: snapshot = live.confirmed;
  $: pins = workbenchDecisionPins(snapshot?.runs ?? [])
    .filter(
      (run) =>
        run.public_run_reference !== conductorRun?.public_run_reference &&
        (conductorLink.kind !== "connected" ||
          run.workflow_revision_hash !== conductorLink.connection.workflowRevisionHash)
    )
    .map((run) => ({
    run,
    workflowName: resolveWorkflowName(run, snapshot?.workflowNames ?? null)
  }));
  $: conversationTranscript = conductorLink.kind === "connected"
    ? conductorTranscript.messages
    : transcript;
  $: conversationRunPath =
    conductorLink.kind === "connected" && conductorRun !== null
      ? runPath(conductorRun.public_run_reference)
      : null;
  $: conversationComplete = conductorLink.kind === "connected" &&
    conductorRun?.state === "COMPLETED" &&
    conductorTranscript.messages.filter((message) => message.speaker === "house").length >=
      conductorLink.connection.maximumRounds;
  // "begins" only holds before the conversation's own first round has
  // actually landed; once one has, the same wait's next message continues it.
  $: connectedComposerHint =
    conductorRun !== null && runHasEnded(conductorRun.state) && conductorRun.state !== "COMPLETED"
      ? conductorConversationCopy.endedHint
      : conductorTranscript.messages.length > 0
        ? conductorConversationCopy.composerHintOngoing
        : conductorConversationCopy.composerHint;
  // The one relative rendering of a failed probe's own instant (`when.ts`
  // owns the formatting); null for every other not-startable reason.
  $: probeFailedAgo =
    conductorLink.kind === "not-startable" &&
    conductorLink.notStartableReason === "provider-probe-failed" &&
    conductorLink.providerProbeObservedAt !== null
      ? ageLabel(conductorLink.providerProbeObservedAt, new Date(), "ago")
      : null;
  // The composer is visibly locked, not merely quiet, whenever the server
  // named a real reason nothing can be sent (#1103), or whenever whether a
  // conductor is even there is still unknown ("reading", #1114): sending
  // then would silently fall into the local-chat branch and be answered as
  // if no conductor existed, and the operator's words would part ways with
  // the conversation the moment the read resolves. This is the one flag both
  // the button's `disabled` attribute and the Enter-key guard in `send`
  // read, so the two can never drift apart.
  $: composerLocked =
    conductorLink.kind === "absent" ||
    conductorLink.kind === "unbound" ||
    conductorLink.kind === "not-startable" ||
    conductorLink.kind === "reading";
  $: if (!pins.some((pin) => pin.run.public_run_reference === expandedPinReference)) {
    expandedPinReference = pins[0]?.run.public_run_reference ?? null;
  }
  // Everything the pins do not already hold as a stage: what is moving, and
  // what waits in a shape this room cannot answer inline (a reconciliation, a
  // run of an older format). Each is one row, one click from its graph -- and
  // nothing stands in both places.
  $: shelf = (snapshot?.runs ?? [])
    .filter((run) => !pins.some((pin) => pin.run.public_run_reference === run.public_run_reference))
    .map((run) => ({
      run,
      standing: runStanding(run.state),
      name: resolveWorkflowName(run, snapshot?.workflowNames ?? null),
      at: run.current_node_id,
      move: humanMove(run.state)
    }));
  /** Runs whose own projection failed (#1042): named, never folded into an empty shelf. */
  $: defective = snapshot?.defective ?? [];
</script>

<section class="workbench surface" aria-labelledby="workbench-title">
  <header class="surface-head">
    <h1 id="workbench-title" tabindex="-1" bind:this={roomHeading}>{wrapDisplayCopy(workbenchPageCopy.title)}</h1>
  </header>

  <!-- A healthy stream says nothing: a permanent "live" badge is chrome and a
       first connect is ordinary loading. A stream merely reconnecting is the
       generic reachability loss the central connection store already names once
       above every room (#700); this line speaks only for what is specific to
       this stream -- a real protocol or terminal failure. -->
  {#if streamStopped(hold)}
    <p class="stream-stopped" role="status">
      <span aria-hidden="true">◇</span>
      {wrapDisplayCopy(connectionLabel(hold))}
    </p>
  {/if}
  {#if hold.stream_failure !== null}
    <ProblemNotice problem={hold.stream_failure} />
  {:else if streamTitle !== null}
    <ProblemNotice title={wrapDisplayCopy(streamTitle)} message={protocolDetail(hold) ?? ""} />
  {/if}
  {#if streamFailureMessage !== null}
    <ProblemNotice message={streamFailureMessage} />
  {/if}
  {#if projectionFailure !== null}
    <ProblemNotice
      message={projectionFailure}
      actionLabel={wrapDisplayCopy(workbenchPageCopy.retryEvent)}
      onAction={retryProjection}
      actionAttributes={{ [workbenchQuestionAttribute]: workbenchQuestions.retryProjection.id }}
    />
  {/if}

  <ReadState
    read={live}
    label={workbenchPageCopy.runsLabel}
    onRetry={() => { void load(); }}
  />
  {#if snapshot !== null && snapshot.workflowNames === null}
    <p class="names-notice" role="status">{wrapDisplayCopy(workbenchPageCopy.workflowNamesUnavailable)}</p>
  {/if}

  <PoisonedJournalDoor
    {mutationJournal}
    bind:poisoned={journalPoisoned}
    onHealed={healJournal}
    focusAfterHeal={roomHeading}
    doorAttributes={{ [workbenchQuestionAttribute]: workbenchQuestions.discardPoisonedJournalDoor.id }}
    confirmAttributes={{ [workbenchQuestionAttribute]: workbenchQuestions.discardPoisonedJournalConfirm.id }}
    cancelAttributes={{ [workbenchQuestionAttribute]: workbenchQuestions.discardPoisonedJournalCancel.id }}
  />

  {#if !journalPoisoned}

  {#if pins.length > 0}
    <section class="needs-you" aria-label={wrapDisplayCopy(workbenchPageCopy.pinnedDecisionsLabel)}>
      <ul class="needs-you-list">
        {#each pins as pin (pin.run.public_run_reference)}
          <li>
            <PinnedDecision
              run={pin.run}
              workflowName={pin.workflowName}
              {cockpitApi}
              {mutationJournal}
              onRunRead={(read) => { absorbRun(read); }}
              {navigate}
              compact={pin.run.public_run_reference !== expandedPinReference}
              onExpand={() => { expandedPinReference = pin.run.public_run_reference; }}
              onJournalPoisoned={() => { journalPoisoned = true; }}
            />
          </li>
        {/each}
      </ul>
    </section>
  {/if}

  <!-- The living shelf: what is moving, one click from its graph. No title
       above it -- a framed row that opens says what it is (ADR 0019 §3). -->
  {#if shelf.length > 0}
    <ul class="living-shelf">
      {#each shelf as row (row.run.public_run_reference)}
        {@const path = runPath(row.run.public_run_reference)}
        <li>
          <a
            class="living-row living-row-{row.standing}"
            href={path}
            onclick={(event) => { event.preventDefault(); navigate(path); }}
          >
            <span class="living-mark" aria-hidden="true">{standingMarks[row.standing]}</span>
            <span class="living-name">{row.name}</span>
            <span class="living-at">{row.at}</span>
            {#if row.move !== null}
              <span class="living-move">{wrapDisplayCopy(row.move)} →</span>
            {/if}
          </a>
        </li>
      {/each}
    </ul>
  {/if}

  <!-- A run whose own projection failed (#1042): named apart from the shelf
       it moves runs it could read on, never folded into an empty state and
       never opened -- there is no graph this room can show for it. -->
  {#if defective.length > 0}
    <ul class="living-shelf" aria-label={wrapDisplayCopy(workbenchPageCopy.defectiveRunsLabel)}>
      {#each defective as row (row.public_run_reference)}
        <DefectiveRunRowItem {row} />
      {/each}
    </ul>
  {/if}

  {#if conversationTranscript.length === 0}
    <div class="workbench-empty card empty-state">
      <h2>{wrapDisplayCopy(workbenchPageCopy.emptyTitle)}</h2>
      {#if conductorLink.kind === "connected"}
        <p>{wrapDisplayCopy(conductorConversationCopy.emptyDescription)}</p>
      {:else if conductorLink.kind === "unbound"}
        <p>{wrapDisplayCopy(workbenchPageCopy.emptyDescriptionUnbound(conductorLink.role))}</p>
        <a
          class="button primary"
          href={settingsPath}
          {...{ [workbenchQuestionAttribute]: workbenchQuestions.emptyOpenSettings.id }}
          onclick={(event) => { event.preventDefault(); navigate(settingsPath); }}
        >{wrapDisplayCopy(workbenchPageCopy.openSettings)}</a>
      {:else if conductorLink.kind === "not-startable"}
        <p>
          {wrapDisplayCopy(
            workbenchPageCopy.emptyDescriptionNotStartable(
              conductorLink.modelId,
              conductorLink.notStartableReason,
              probeFailedAgo
            )
          )}
        </p>
        <a
          class="button primary"
          href={settingsPath}
          {...{ [workbenchQuestionAttribute]: workbenchQuestions.emptyOpenSettings.id }}
          onclick={(event) => { event.preventDefault(); navigate(settingsPath); }}
        >{wrapDisplayCopy(workbenchPageCopy.openSettings)}</a>
      {:else}
        <p>{wrapDisplayCopy(workbenchPageCopy.emptyDescription)}</p>
        <a
          class="button primary"
          href={catalogPath}
          {...{ [workbenchQuestionAttribute]: workbenchQuestions.emptyStart.id }}
          onclick={(event) => { event.preventDefault(); navigate(catalogPath); }}
        >{wrapDisplayCopy(workbenchPageCopy.emptyStart)}</a>
      {/if}
    </div>
  {:else}
    <ol class="conversation" aria-label={wrapDisplayCopy(workbenchPageCopy.transcriptLabel)}>
      {#each conversationTranscript as message (message.id)}
        <li class="conversation-line conversation-line-{message.speaker}">
          <p class="conversation-message">
            <span class="conversation-speaker">{wrapDisplayCopy(speakerLabels[message.speaker])}</span>
            {message.text}
          </p>
        </li>
      {/each}
    </ol>
    <!-- One link for the whole conversation, because the whole conversation is
         one run (#658). Per line it was the episode model speaking, where each
         message had a run of its own; here it would repeat the same href once
         per round, up to the loop's ceiling. -->
    {#if conversationRunPath !== null}
      <p class="conversation-run">
        <a
          class="conversation-run-link"
          href={conversationRunPath}
          onclick={(event) => {
            event.preventDefault();
            navigate(conversationRunPath);
          }}
        >{wrapDisplayCopy(conductorChatCopy.openEpisode)}</a>
      </p>
    {/if}
  {/if}

  <form class="composer" aria-label={wrapDisplayCopy(workbenchPageCopy.composerRegionLabel)} onsubmit={send}>
    <label class="composer-label" for="workbench-message">
      {wrapDisplayCopy(workbenchPageCopy.composerLabel)}
    </label>
    <div class="composer-row">
      <textarea
        id="workbench-message"
        rows="2"
        bind:value={typed}
        bind:this={composer}
        onkeydown={keydown}
      ></textarea>
      <button
        class="primary"
        type="submit"
        disabled={$connectionState === "reconnecting" || conductorDeliveryBusy || composerLocked ||
          (conductorRun !== null &&
            conductorRun.state !== "WAITING_INPUT" &&
            !runHasEnded(conductorRun.state))}
        {...{ [workbenchQuestionAttribute]: workbenchQuestions.saySomething.id }}
      >{wrapDisplayCopy(workbenchPageCopy.send)}</button>
    </div>
    {#if $connectionState === "reconnecting"}
      <!-- The ear always names its own state in one sentence (HEART, "The
           ear"): while the connection is lost that sentence is this one,
           replacing whatever it would otherwise say, and no separate banner
           repeats it above (#700, App.svelte). -->
      <p class="composer-hint">{wrapDisplayCopy(restartNoticeCopy)}</p>
    {:else if conductorLink.kind === "connected"}
      <p class="composer-hint">{wrapDisplayCopy(connectedComposerHint)}</p>
    {:else if conductorLink.kind === "reading"}
      <p class="composer-hint">{wrapDisplayCopy(workbenchPageCopy.composerHintReading)}</p>
    {:else if conductorLink.kind === "absent"}
      <p class="composer-hint">{wrapDisplayCopy(workbenchPageCopy.composerHint)}</p>
    {:else if conductorLink.kind === "unbound" && conversationTranscript.length > 0}
      <!-- The empty room's own card already names this exact reason (above,
           `emptyDescriptionUnbound`) whenever the conversation is empty; this
           hint only repeats it when that card is not on screen -- the same
           complementary rule as "not-startable" below (#1103). -->
      <p class="composer-hint">{wrapDisplayCopy(workbenchPageCopy.composerHintUnbound(conductorLink.role))}</p>
    {:else if conductorLink.kind === "not-startable" && conversationTranscript.length > 0}
      <!-- The empty room's own card already names this exact reason (below,
           `emptyDescriptionNotStartable`) whenever the conversation is empty;
           this hint only repeats it when that card is not on screen -- a
           not-startable conductor reached after "unreadable" had already
           taken a locally-answered turn, then the connection recovered and
           resolved to a real reason (#700, #1103). -->
      <p class="composer-hint">
        {wrapDisplayCopy(
          workbenchPageCopy.composerHintNotStartable(
            conductorLink.modelId,
            conductorLink.notStartableReason,
            probeFailedAgo
          )
        )}
      </p>
    {:else if conductorLink.kind === "unreadable"}
      <p class="composer-hint">{wrapDisplayCopy(conductorChatCopy.connectionUnknown)}</p>
    {/if}
    {#if conversationComplete}
      <p class="composer-hint" role="status">{wrapDisplayCopy(conductorConversationCopy.complete)}</p>
    {/if}
    {#if conductorDeliveryFailure !== null}
      <p class="composer-hint" role="status">{conductorDeliveryFailure}</p>
    {/if}
  </form>
  {/if}
</section>

<style>
  /* The pinned region and the ear are the two fixtures of the Workbench: they
     hold to the top and bottom of the stage while the conversation scrolls
     between them, so an open decision never leaves the screen (issue #580). The
     stage's own ground shows through, so each fixture wears it to occlude the
     lines sliding under its edge. */
  .needs-you {
    position: sticky;
    top: 0;
    z-index: 1;
    display: grid;
    gap: var(--space-3);
    min-height: 0;
    /* One expanded stage and about three compact decisions keep the ear and
       conversation in the 390px room; more remains reachable by this stack's
       own scroll, whose fade is the promised affordance. */
    max-height: calc(var(--tap) * 7 + var(--space-3) * 3);
    overflow-y: auto;
    mask-image: linear-gradient(to bottom, var(--mask-opaque) calc(100% - var(--space-3)), transparent);
    -webkit-mask-image: linear-gradient(to bottom, var(--mask-opaque) calc(100% - var(--space-3)), transparent);
    padding-block: var(--space-3);
    border-bottom: var(--edge) solid var(--line);
    background: var(--ground);
  }

  .needs-you-list {
    display: grid;
    gap: var(--space-3);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .needs-you-list li {
    min-width: 0;
  }

  .names-notice {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-xs);
  }

  .stream-stopped {
    margin: 0;
    color: var(--signal-failure);
  }

  .living-shelf {
    display: grid;
    gap: var(--space-2);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  /* A framed row that opens: what lies on the living shelf is still in hand
     (ADR 0019 §3), unlike History's ruled lines. */
  .living-row {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--space-2) var(--space-3);
    min-height: var(--tap);
    border: var(--edge) solid var(--line);
    border-radius: var(--r-lg);
    padding: var(--space-3) var(--space-4);
    background: var(--panel2);
    color: inherit;
    font-size: var(--text-sm);
    text-decoration: none;
  }

  .living-row:hover,
  .living-row:focus-visible {
    border-color: var(--accent);
  }

  .living-row-running .living-mark {
    color: var(--signal-live);
  }

  .living-row-waiting .living-mark {
    color: var(--signal-attention-mark);
  }

  .living-name {
    font-weight: var(--weight-strong);
    overflow-wrap: anywhere;
  }

  /* Which hand is at work, as the node's own name -- the fact, not a state
     word beside a colour that already says it. */
  .living-at {
    margin-left: auto;
    color: var(--ink-dim);
    font-size: var(--text-xs);
  }

  /* The move a person still owes this run, where there is one: the row's own
     door already opens it, so the words name the move, not the state. */
  .living-move {
    color: var(--accent);
    font-size: var(--text-xs);
    font-weight: var(--weight-strong);
  }

  .conversation {
    display: grid;
    gap: var(--space-3);
    max-width: var(--reading-width);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .conversation-line {
    display: flex;
    min-width: 0;
  }

  .conversation-line-you {
    justify-content: flex-end;
  }

  .conversation-message {
    display: grid;
    gap: var(--space-1);
    max-width: 92%;
    margin: 0;
    border: var(--edge) solid var(--line);
    border-radius: var(--r-lg);
    padding: var(--space-3) var(--space-4);
    background: var(--panel2);
    font-size: var(--text-sm);
    overflow-wrap: anywhere;
    /* A conductor reply may carry its own line breaks; they are part of what
       it said. */
    white-space: pre-line;
  }

  .conversation-run {
    max-width: var(--reading-width);
    margin: var(--space-2) 0 0;
  }

  .conversation-run-link {
    font-size: var(--text-xs);
  }

  /* Your own line is the paper one shade deeper, mixed from the ground the
     workshop already uses rather than a grey pasted onto a warm house. */
  .conversation-line-you .conversation-message {
    border-color: color-mix(in srgb, var(--ink) 20%, var(--line));
    background: var(--chip);
  }

  .conversation-speaker {
    color: var(--ink-dim);
    font-size: var(--text-2xs);
    font-weight: var(--weight-strong);
  }

  .composer {
    position: sticky;
    bottom: 0;
    display: grid;
    gap: var(--space-2);
    max-width: var(--reading-width);
    padding-top: var(--space-3);
    border-top: var(--edge) solid var(--line);
    background: var(--ground);
  }

  .composer-label {
    color: var(--ink-dim);
    font-size: var(--text-xs);
    font-weight: var(--weight-strong);
  }

  .composer-row {
    display: flex;
    align-items: flex-end;
    gap: var(--space-3);
  }

  .composer-row textarea {
    flex: 1;
    font-family: var(--sans);
    font-size: var(--text-sm);
  }

  .composer-hint {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-xs);
  }

  @media (max-width: 48rem) {
    .composer {
      gap: var(--space-1);
      padding-top: var(--space-1);
    }

    /* The sticky rail overlays the stream at this narrow height. Keep the
       first visible conversation line below its fade without spending more
       room in the document flow that anchors the composer. */
    .needs-you ~ .conversation {
      translate: 0 var(--space-5);
    }
  }
</style>
