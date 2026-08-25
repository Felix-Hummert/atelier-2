<script lang="ts">
  import { onMount } from "svelte";

  import {
    isRunV3,
    type AnyRun,
    type CockpitApi,
    type RunEvent,
    type RunEventSubscription
  } from "../api/client";
  import BoardWaitingAnswer from "../components/BoardWaitingAnswer.svelte";
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
  import { BOARD_GROUPS, projectBoardGroups, type BoardGroups } from "../lib/boardRows";
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
  import { readEveryRevision, readEveryRun } from "../lib/runPages";
  import { standingMarks, standingWords } from "../lib/runState";
  import { studioPageCopy } from "../lib/studioPageCopy";
  import { studioQuestions } from "../lib/studioQuestions";
  import {
    connectionLabel,
    protocolDetail,
    protocolTitle,
    streamStopped
  } from "../lib/streamStatus";
  import { boardBadgeCounts } from "../lib/workshop";

  export let cockpitApi: CockpitApi;
  export let mutationJournal: MutationJournal;
  export let navigate: (path: string) => void;

  type StudioHome = {
    runs: AnyRun[];
    /** Null when the described catalog could not be read this round: enrichment, not a gate. */
    workflowNames: ReadonlyMap<string, string | null> | null;
  };

  type StudioReadFailure =
    | { kind: "unavailable"; title: string }
    | { kind: "incomplete"; title: string };

  const groupTitle = {
    needsYou: studioPageCopy.needsYou,
    running: studioPageCopy.running
  } as const satisfies Record<keyof BoardGroups, string>;

  let home: RetainedRead<StudioHome, StudioReadFailure> =
    retainedRead<StudioHome, StudioReadFailure>();
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
    const begun = beginRead(home);
    home = begun.read;
    try {
      const [started, waitingInput, waitingReconciliation, revisions] =
        await Promise.all([
          readEveryRun((after) => cockpitApi.listRuns(after, "STARTED")),
          readEveryRun((after) => cockpitApi.listRuns(after, "WAITING_INPUT")),
          readEveryRun((after) => cockpitApi.listRuns(after, "WAITING_RECONCILIATION")),
          readEveryRevision((after) => cockpitApi.listWorkflowRevisions(after))
        ]);
      // The Board reads only the non-terminal run states -- what still moves
      // or wants a human now (operator ruling #667). A terminal run belongs
      // to History instead, so it is never asked for here. Any of these
      // three lists incomplete stops the confirm, same discipline as before.
      // The catalog read is enrichment over that truth, never a gate on it --
      // a run still confirms with its own real fields on a failed catalog
      // read, falling back to run_id honestly instead of losing the whole
      // board.
      const runReadings = [started, waitingInput, waitingReconciliation];
      if (runReadings.some((reading) => !reading.complete)) {
        home = failRead(home, begun.generation, {
          kind: "incomplete",
          title: wrapDisplayCopy(studioPageCopy.runsIncomplete)
        });
        return;
      }
      const known = mergedRuns(home.confirmed?.runs ?? [], [
        ...started.runs,
        ...waitingInput.runs,
        ...waitingReconciliation.runs
      ]);
      const workflowNames = revisions.complete
        ? new Map(revisions.revisions.map((revision) => [revision.workflow_revision_hash, revision.name]))
        : null;
      confirm(begun.generation, { runs: known, workflowNames });
    } catch {
      home = failRead(home, begun.generation, {
        kind: "unavailable",
        title: wrapDisplayCopy(studioPageCopy.runsUnavailable)
      });
    }
  }

  function confirm(generation: number, confirmed: StudioHome): void {
    const before = home;
    home = confirmRead(home, generation, confirmed);
    if (home !== before) publishBadges(confirmed.runs);
  }

  function mergedRuns(currentRuns: readonly AnyRun[], runs: readonly AnyRun[]): AnyRun[] {
    const known = [...currentRuns];
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
    return known;
  }

  function upsertRuns(runs: readonly AnyRun[]): void {
    const merged = mergedRuns(home.confirmed?.runs ?? [], runs);
    home = updateConfirmed(home, {
      runs: merged,
      workflowNames: home.confirmed?.workflowNames ?? null
    });
    publishBadges(merged);
  }

  /** The rail's Board badges: the last confirmed read, and no earlier. */
  function publishBadges(runs: readonly AnyRun[]): void {
    const groups = projectBoardGroups(runs, new Map());
    boardBadgeCounts.set({ needsYou: groups.needsYou.length, running: groups.running.length });
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

  function open(path: string) {
    return (event: Event) => {
      event.preventDefault();
      navigate(path);
    };
  }

  $: snapshot = home.confirmed;
  $: groups = snapshot === null ? null : projectBoardGroups(snapshot.runs, snapshot.workflowNames);
  $: empty =
    groups !== null &&
    groups.needsYou.length === 0 &&
    groups.running.length === 0 &&
    hold.connection === "live" &&
    !streamStopped(hold);
  // Gates the empty state's one action: a projection the board could not
  // apply (a wait it never confirmed) makes "nothing is running" unproven,
  // so the board withholds the next action rather than act on a guess.
  $: canStart = !attentionStopped(hold) && projectionFailure === null;
  $: streamTitle = protocolTitle(hold);
</script>

<section class="board-page surface" aria-labelledby="board-title">
  <header class="surface-head">
    <h1 id="board-title">{wrapDisplayCopy(studioPageCopy.title)}</h1>
  </header>

  <!-- A healthy stream says nothing: a permanent "live" badge is chrome and a
       first connect is ordinary loading. A stream merely reconnecting is the
       generic reachability loss the central connection store already names
       once, above every room (#700); this line speaks only for what is
       specific to this stream -- a real protocol or terminal failure
       (operator ruling 23.08.). -->
  {#if streamStopped(hold)}
    <p class="connection connection-{hold.connection} connection-problem" role="status">
      <span aria-hidden="true">◇</span>
      {wrapDisplayCopy(connectionLabel(hold))}
    </p>
  {/if}
  {#if hold.stream_failure !== null}
    <ProblemNotice problem={hold.stream_failure} />
  {:else if streamTitle !== null}
    <ProblemNotice title={wrapDisplayCopy(streamTitle)} message={protocolDetail(hold) ?? ""} />
  {/if}

  {#if projectionFailure !== null}
    <ProblemNotice message={projectionFailure} />
    <button
      type="button"
      data-studio-question={studioQuestions.retryProjection.id}
      onclick={retryProjection}
    >Retry</button>
  {/if}
  <ReadState
    read={home}
    label={studioQuestions.reloadStudioRuns.readLabel}
    onRetry={() => { void load(); }}
  />
  {#if failureMessage !== null}
    <ProblemNotice message={failureMessage} />
  {/if}
  {#if snapshot !== null && snapshot.workflowNames === null}
    <p class="catalog-notice" role="status">{wrapDisplayCopy(studioPageCopy.workflowNamesUnavailable)}</p>
  {/if}

  {#if groups !== null}
    {#if empty}
      <div class="board-empty card empty-state">
        <h2>{wrapDisplayCopy(studioPageCopy.emptyTitle)}</h2>
        <p>{wrapDisplayCopy(studioPageCopy.emptyDescription)}</p>
        {#if canStart}
          <a
            class="button primary empty-start"
            href="/atelier/workflows"
            data-studio-question={studioQuestions.emptyStart.id}
            onclick={open("/atelier/workflows")}
          >{wrapDisplayCopy(studioPageCopy.emptyStart)}</a>
        {/if}
      </div>
    {:else}
      {#each BOARD_GROUPS as group (group)}
        {#if groups[group].length > 0}
          <section class="board-group" aria-labelledby={`board-group-${group}`}>
            <h2 class="board-group-title" id={`board-group-${group}`}>
              {wrapDisplayCopy(groupTitle[group])} · {groups[group].length}
            </h2>
            <ul class="board-rows">
              {#each groups[group] as row (row.run.public_run_reference)}
                {@const inlineAnswer = row.status.kind === "waitingInput" && isRunV3(row.run)}
                <li>
                  <div class="board-row board-row-{row.standing}">
                    <a
                      class="board-row-link"
                      href={runPath(row.run.public_run_reference)}
                      onclick={open(runPath(row.run.public_run_reference))}
                    >
                      <span class="row-mark" aria-hidden="true">{standingMarks[row.standing]}</span>
                      <span class="row-name">{row.name}</span>
                      <span class="row-status">
                        {#if row.status.kind === "waitingInput" || row.status.kind === "waitingReconciliation"}{wrapDisplayCopy(standingWords.waiting)}
                        {:else}{wrapDisplayCopy(standingWords.running)} · {row.status.nodeId}{/if}
                      </span>
                      {#if row.miniPipeline !== null}
                        <span class="row-pipeline" aria-hidden="true">
                          {#each row.miniPipeline as dot (dot.nodeId)}
                            <i class="pipe-dot pipe-dot-{dot.state}"></i>
                          {/each}
                        </span>
                      {/if}
                      {#if inlineAnswer}
                        <!-- The inline "Answer here" toggle below is the primary
                             decision door on this card; this quiet run-path link
                             is the deliberately subordinate secondary door to the
                             whole run, not a second equally-weighted answer
                             control (Leonardo-Gate 23.08.). -->
                        <span class="row-action row-action-quiet">{wrapDisplayCopy(studioPageCopy.openRun)} →</span>
                      {:else if row.humanMove !== null}
                        <span class="row-action">{wrapDisplayCopy(row.humanMove)} →</span>
                      {/if}
                    </a>
                    {#if row.status.kind === "waitingInput" && isRunV3(row.run)}
                      <BoardWaitingAnswer
                        run={row.run}
                        {cockpitApi}
                        {mutationJournal}
                        onRunRead={(read) => upsertRuns([read])}
                        {navigate}
                      />
                    {/if}
                  </div>
                </li>
              {/each}
            </ul>
          </section>
        {/if}
      {/each}
    {/if}
  {/if}
</section>

<style>
  .board-group {
    min-width: 0;
  }

  .connection {
    margin: 0;
  }

  .catalog-notice {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-xs);
  }

  .board-group-title {
    margin: var(--space-4) 0 var(--space-2);
    font-size: var(--text-2xs);
    letter-spacing: var(--tracking-label);
    text-transform: uppercase;
    color: var(--ink-dim);
  }

  .board-rows {
    display: grid;
    min-width: 0;
    gap: var(--space-2);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .board-rows li {
    min-width: 0;
  }

  /* The card frame: everything a row shows -- the navigable summary and, for
     a boolean/enum wait, its inline answer -- lives inside this one bordered
     box, never a control floating between cards (Leonardo-Gate 23.08.). */
  .board-row {
    min-width: 0;
    border: var(--edge) solid var(--line);
    border-radius: var(--r-lg);
    background: var(--panel2);
    box-shadow: var(--shadow);
  }

  .board-row-link {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    min-width: 0;
    gap: var(--space-2) var(--space-3);
    min-height: var(--tap);
    padding: var(--space-3) var(--space-4);
    color: inherit;
    text-decoration: none;
  }

  .row-mark {
    flex: none;
  }

  .board-row-running .row-mark {
    color: var(--signal-live);
  }

  .board-row-waiting .row-mark {
    color: var(--signal-attention-mark);
  }

  .row-name {
    flex: none;
    font-weight: var(--weight-strong);
    max-width: var(--name-column);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .row-status {
    flex: 1;
    min-width: 0;
    color: var(--ink-dim);
    font-size: var(--text-sm);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .row-pipeline {
    display: inline-flex;
    flex: none;
    gap: var(--space-1);
    align-items: center;
  }

  /* Colours mirror StateMark's own state-to-token map (styles.css .state-*),
     so a node reads the same colour on the Board's mini pipeline as it does
     on the run page itself -- one convention, not two. */
  .pipe-dot {
    display: inline-block;
    width: var(--dot);
    height: var(--dot);
    border-radius: 50%;
  }

  .pipe-dot-queued,
  .pipe-dot-succeeded,
  .pipe-dot-cancelled {
    background: var(--signal-quiet);
  }

  .pipe-dot-working {
    background: var(--signal-live);
  }

  .pipe-dot-needs_you {
    background: var(--signal-attention-mark);
  }

  .pipe-dot-failed,
  .pipe-dot-interrupted {
    background: var(--signal-failure);
  }

  .row-action {
    flex: none;
    font-weight: var(--weight-strong);
    font-size: var(--text-xs);
    color: var(--accent);
  }

  /* The run-path door stays visible once a boolean/enum wait offers its own
     inline "Answer here" -- but quiet and unbolded, so it reads as the
     subordinate way to the whole run, not a second answer control of equal
     weight (Leonardo-Gate 23.08.). */
  .row-action-quiet {
    font-weight: var(--weight-medium);
    color: var(--ink-dim);
  }

  /* Below this width a row cannot hold its sentence beside its marks, so the
     sentence takes its own line. What a run is doing is never the part that
     gets cut (operator ruling 23.08.). */
  @media (max-width: 32rem) {
    .row-name {
      max-width: var(--name-column-narrow);
    }

    .row-status {
      flex-basis: 100%;
      overflow: visible;
      white-space: normal;
      text-overflow: clip;
    }
  }
</style>
