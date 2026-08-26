<script lang="ts">
  import { onMount, tick } from "svelte";

  import {
    isRunV3,
    type AnyRun,
    type CockpitApi,
    type RunEvent,
    type RunEventSubscription,
    type RunV3
  } from "../api/client";
  import PinnedDecision from "../components/PinnedDecision.svelte";
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
  import { conductorChatCopy } from "../lib/conductorChatCopy";
  import {
    resolveConductorConnection,
    sendConductorMessage,
    type ConductorConnection
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
  import { runPath } from "../lib/route";
  import { newestReadOfEachRun, resolveWorkflowName } from "../lib/runList";
  import { readEveryRevision, readEveryRun } from "../lib/runPages";
  import { humanMove, runStanding, standingMarks } from "../lib/runState";
  import {
    connectionLabel,
    protocolDetail,
    protocolTitle,
    streamStopped
  } from "../lib/streamStatus";
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
    runs: AnyRun[];
    /** Null when the described catalog could not be read this round: enrichment, not a gate. */
    workflowNames: ReadonlyMap<string, string | null> | null;
  };

  type ReadFailure =
    | { kind: "unavailable"; title: string }
    | { kind: "incomplete"; title: string };

  /**
   * Whether a conductor reads this ear. "reading" is the moment before the
   * answer is known and "unreadable" the honest state when the reads themselves
   * failed -- neither pretends "connected" or "absent", because either would be
   * a guess dressed as a fact.
   */
  type ConductorLink =
    | { kind: "reading" }
    | { kind: "unreadable" }
    | { kind: "absent" }
    | { kind: "connected"; connection: ConductorConnection };

  let live: RetainedRead<WorkbenchRuns, ReadFailure> = retainedRead<WorkbenchRuns, ReadFailure>();
  let hold: AttentionHold = startAttentionHold();
  let stream: RunEventSubscription | null = null;
  let streamFailureMessage: string | null = null;
  let projectionFailure: string | null = null;
  let disposed = false;
  let eventQueue: Promise<void> = Promise.resolve();
  const pendingEvents: RunEvent[] = [];
  let transcript: readonly ChatMessage[] = currentChatTranscript();
  let typed = "";
  let composer: { focus(): void };
  let conductorLink: ConductorLink = { kind: "reading" };

  const speakerLabels: Record<ChatMessage["speaker"], string> = {
    you: workbenchPageCopy.youLabel,
    house: workbenchPageCopy.houseLabel
  };

  const catalogPath = WORKSHOP_DESTINATION.catalog.path;

  onMount(() => {
    void load();
    holdAttention();
    void resolveConductor();
    const unsubscribe = subscribeChatTranscript((next) => {
      const settledALine =
        transcript.some((line) => line.pending) && !next.some((line) => line.pending);
      transcript = next;
      // A settled episode may have started runs or opened waits; the room
      // re-reads so a new decision does not wait for the next visit.
      if (settledALine) void load();
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
      unsubscribe();
      unsubscribeConnection();
    };
  });

  async function resolveConductor(): Promise<void> {
    try {
      const connection = await resolveConductorConnection(cockpitApi);
      conductorLink =
        connection === null ? { kind: "absent" } : { kind: "connected", connection };
    } catch {
      conductorLink = { kind: "unreadable" };
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
   * These runs are not streamed: a decision that opens while the operator is
   * already sitting here appears on the next read, not the moment it opens.
   * Consuming the live attention stream is a named successor gap.
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
      confirm(begun.generation, {
        runs: newestReadOfEachRun(runReadings.flatMap((reading) => reading.runs)),
        workflowNames
      });
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
  function publishCount(runs: readonly AnyRun[]): void {
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
  function absorbRun(read: AnyRun): boolean {
    const confirmed = live.confirmed;
    if (confirmed === null) return false;
    const others = confirmed.runs.filter(
      (run) => run.public_run_reference !== read.public_run_reference
    );
    const runs = runHasMoved(read) ? others : [...others, read];
    live = updateConfirmed(live, { ...confirmed, runs });
    publishCount(runs);
    return true;
  }

  function runHasMoved(run: AnyRun): boolean {
    const standing = runStanding(run.state);
    return standing !== "waiting" && standing !== "running";
  }

  /**
   * A connected conductor turns the message into one episodic run whose reply
   * settles into this conversation; every other state keeps the standing
   * honest refusal -- including "unreadable", where nothing was started is
   * still the whole truth.
   *
   * A lost connection (#700) keeps the message in the box instead: the send
   * button is disabled the same moment, so this guard only catches the
   * keyboard's Enter shortcut racing that disable.
   */
  async function send(event: Event): Promise<void> {
    event.preventDefault();
    if (typed.trim().length === 0 || $connectionState === "reconnecting") return;
    if (conductorLink.kind === "connected") {
      sendConductorMessage(cockpitApi, conductorLink.connection, typed);
    } else {
      sendChatTurn(typed);
    }
    transcript = currentChatTranscript();
    typed = "";
    await tick();
    composer.focus();
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
  $: pins = (snapshot?.runs ?? [])
    .filter((run): run is RunV3 => isRunV3(run) && run.state === "WAITING_INPUT")
    .map((run) => ({
      run,
      workflowName: resolveWorkflowName(run, snapshot?.workflowNames ?? null)
    }));
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
      at: isRunV3(run) ? run.current_node_id : run.current_node.node_id,
      move: humanMove(run.state)
    }));
</script>

<section class="workbench surface" aria-labelledby="workbench-title">
  <header class="surface-head">
    <h1 id="workbench-title">{wrapDisplayCopy(workbenchPageCopy.title)}</h1>
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
    <ProblemNotice message={projectionFailure} />
    <button
      type="button"
      {...{ [workbenchQuestionAttribute]: workbenchQuestions.retryProjection.id }}
      onclick={retryProjection}
    >{wrapDisplayCopy(workbenchPageCopy.retryEvent)}</button>
  {/if}

  <ReadState
    read={live}
    label={workbenchQuestions.reloadWorkbenchRuns.readLabel}
    onRetry={() => { void load(); }}
  />
  {#if snapshot !== null && snapshot.workflowNames === null}
    <p class="names-notice" role="status">{wrapDisplayCopy(workbenchPageCopy.workflowNamesUnavailable)}</p>
  {/if}

  {#if pins.length > 0}
    <div class="needs-you">
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
            />
          </li>
        {/each}
      </ul>
    </div>
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

  {#if transcript.length === 0}
    <div class="workbench-empty card empty-state">
      <h2>{wrapDisplayCopy(workbenchPageCopy.emptyTitle)}</h2>
      {#if conductorLink.kind === "connected"}
        <p>{wrapDisplayCopy(conductorChatCopy.emptyDescription)}</p>
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
      {#each transcript as message (message.id)}
        <li class="conversation-line conversation-line-{message.speaker}">
          <p class="conversation-message" class:conversation-message-pending={message.pending}>
            <span class="conversation-speaker">{wrapDisplayCopy(speakerLabels[message.speaker])}</span>
            {message.text}
            {#if message.runReference !== undefined}
              {@const episodePath = runPath(message.runReference)}
              <a
                class="conversation-run-link"
                href={episodePath}
                onclick={(event) => {
                  event.preventDefault();
                  navigate(episodePath);
                }}
              >{wrapDisplayCopy(conductorChatCopy.openEpisode)}</a>
            {/if}
          </p>
        </li>
      {/each}
    </ol>
  {/if}

  <form class="composer" onsubmit={send}>
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
        disabled={$connectionState === "reconnecting"}
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
      <p class="composer-hint">{wrapDisplayCopy(conductorChatCopy.composerHint)}</p>
    {:else if conductorLink.kind === "absent"}
      <p class="composer-hint">{wrapDisplayCopy(workbenchPageCopy.composerHint)}</p>
    {:else if conductorLink.kind === "unreadable"}
      <p class="composer-hint">{wrapDisplayCopy(conductorChatCopy.connectionUnknown)}</p>
    {/if}
  </form>
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

  /* A line still waiting for its episode is visibly provisional, nothing more:
     dimming is state, the settled text is the event. */
  .conversation-message-pending {
    color: var(--ink-dim);
    font-style: italic;
  }

  .conversation-run-link {
    justify-self: start;
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
</style>
