<script lang="ts">
  import { onMount } from "svelte";

  import type { AnyRun, CockpitApi, WorkflowRevisionDetail } from "../api/client";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import {
    HISTORY_PERIOD_DAYS,
    hasTimestamplessRows,
    projectHistoryRows,
    withinHistoryPeriod,
    type HistoryRow
  } from "../lib/historyRows";
  import { meaningOf, type RowMeaning } from "../lib/historyMeaning";
  import { historyPageCopy, periodChipLabel } from "../lib/historyPageCopy";
  import {
    beginRead,
    confirmRead,
    failRead,
    retainedRead,
    type RetainedRead
  } from "../lib/readResource";
  import { runPath } from "../lib/route";
  import { standingWords } from "../lib/runState";
  import { v3WorkflowGraph, workflowRevisionsOf, type WorkflowGraphV3 } from "../lib/runList";
  import { readEveryRun } from "../lib/runPages";
  import { ageLabel, exactLocal } from "../lib/when";

  export let cockpitApi: CockpitApi;
  export let navigate: (path: string) => void;

  interface HistorySnapshot {
    runs: AnyRun[];
    workflowNames: ReadonlyMap<string, string>;
    meanings: ReadonlyMap<string, RowMeaning>;
  }

  type HistoryReadFailure =
    | { kind: "unavailable"; title: string }
    | { kind: "incomplete"; title: string };

  let history: RetainedRead<HistorySnapshot, HistoryReadFailure> =
    retainedRead<HistorySnapshot, HistoryReadFailure>();
  const now = new Date();

  onMount(() => {
    void load();
  });

  async function load(): Promise<void> {
    const begun = beginRead(history);
    history = begun.read;
    try {
      const [completed, failed] = await Promise.all([
        readEveryRun((after) => cockpitApi.listRuns(after, "COMPLETED")),
        readEveryRun((after) => cockpitApi.listRuns(after, "FAILED"))
      ]);
      if (!completed.complete || !failed.complete) {
        history = failRead(history, begun.generation, {
          kind: "incomplete",
          title: historyPageCopy.listIncomplete
        });
        return;
      }
      const runs = [...completed.runs, ...failed.runs];
      const revisions = await workflowRevisionsOf(runs, (hash) =>
        cockpitApi.getWorkflowRevision(hash)
      );
      const workflowNames = new Map(
        [...revisions].map(([hash, revision]) => [hash, v3WorkflowGraph(revision).name])
      );
      const rows = projectHistoryRows(runs, workflowNames);
      // Only the rows the period chip actually shows are worth a live node
      // read: an old row a person will never see under "7 days" would spend a
      // fetch nobody reads (this filter is the same `withinHistoryPeriod` the
      // render below reapplies over the same, unchanging `now`).
      const visible = rows.filter((row) => withinHistoryPeriod(row, now));
      const meanings = await readMeanings(visible, revisions, cockpitApi);
      history = confirmRead(history, begun.generation, { runs, workflowNames, meanings });
    } catch {
      history = failRead(history, begun.generation, {
        kind: "unavailable",
        title: historyPageCopy.listUnavailable
      });
    }
  }

  async function readMeanings(
    rows: readonly HistoryRow[],
    revisions: ReadonlyMap<string, WorkflowRevisionDetail>,
    api: CockpitApi
  ): Promise<ReadonlyMap<string, RowMeaning>> {
    const entries = await Promise.all(
      rows.map(async (row) => {
        const revision = revisions.get(row.run.workflow_revision_hash);
        const graph: WorkflowGraphV3 | null =
          revision === undefined ? null : v3WorkflowGraph(revision);
        const meaning = await meaningOf(row, graph, api);
        return [row.run.public_run_reference, meaning] as const;
      })
    );
    return new Map(entries);
  }

  function open(publicReference: string) {
    return (event: Event) => {
      event.preventDefault();
      navigate(runPath(publicReference));
    };
  }

  $: rows = history.confirmed === null
    ? []
    : projectHistoryRows(history.confirmed.runs, history.confirmed.workflowNames);
  $: visibleRows = rows.filter((row) => withinHistoryPeriod(row, now));
  $: showTimestamplessHint = hasTimestamplessRows(visibleRows);
  $: meanings = history.confirmed?.meanings ?? new Map<string, RowMeaning>();
</script>

<section class="history-page surface" aria-labelledby="history-title">
  <header class="surface-head history-head">
    <div>
      <h1 id="history-title">{wrapDisplayCopy(historyPageCopy.title)}</h1>
    </div>
    <span class="period-note">{periodChipLabel(HISTORY_PERIOD_DAYS)}</span>
  </header>

  {#if history.confirmed === null && history.request.state === "loading"}
    <p class="status" role="status">{wrapDisplayCopy(historyPageCopy.looking)}</p>
  {:else if history.request.state === "failed"}
    <p class="failure" role="alert">{history.request.failure.title}</p>
    <button type="button" onclick={() => { void load(); }}>{wrapDisplayCopy(historyPageCopy.retry)}</button>
  {/if}

  {#if history.confirmed !== null}
    {#if visibleRows.length === 0}
      <div class="history-empty card empty-state">
        <h2>{wrapDisplayCopy(historyPageCopy.emptyTitle)}</h2>
        <p>{wrapDisplayCopy(historyPageCopy.emptyDescription)}</p>
        <a
          class="button primary"
          href="/atelier/workflows"
          onclick={(event) => { event.preventDefault(); navigate("/atelier/workflows"); }}
        >{wrapDisplayCopy(historyPageCopy.emptyNext)}</a>
      </div>
    {:else}
      <div class="history-head-row" aria-hidden="true">
        <span class="col-name">{wrapDisplayCopy(historyPageCopy.columnName)}</span>
        <span class="col-when">{wrapDisplayCopy(historyPageCopy.columnWhen)}</span>
        <span class="col-purpose">{wrapDisplayCopy(historyPageCopy.columnPurpose)}</span>
        <span class="col-result">{wrapDisplayCopy(historyPageCopy.columnResult)}</span>
        <span class="col-duration">{wrapDisplayCopy(historyPageCopy.columnDuration)}</span>
      </div>
      <ul class="history-rows">
        {#each visibleRows as row (row.run.public_run_reference)}
          {@const meaning = meanings.get(row.run.public_run_reference) ?? null}
          <li>
            <a
              class="history-row history-row-{row.result.kind}"
              href={runPath(row.run.public_run_reference)}
              onclick={open(row.run.public_run_reference)}
            >
              <span class="row-name">
                <span class="visually-hidden">{wrapDisplayCopy(historyPageCopy.columnName)}: </span>
                {row.name}
              </span>
              <span class="row-when">
                <span class="visually-hidden">{wrapDisplayCopy(historyPageCopy.columnWhen)}: </span>
                {#if row.activityAt !== null}
                  <time datetime={row.activityAt} title={exactLocal(row.activityAt)}>
                    {ageLabel(row.activityAt, now, "ago")}
                  </time>
                {:else}
                  {wrapDisplayCopy(historyPageCopy.whenNotRecorded)}
                {/if}
              </span>
              <span class="row-purpose">
                <span class="visually-hidden">{wrapDisplayCopy(historyPageCopy.columnPurpose)}: </span>
                {meaning?.purpose ?? wrapDisplayCopy(historyPageCopy.purposeNone)}
              </span>
              <span class="row-result">
                <span class="visually-hidden">{wrapDisplayCopy(historyPageCopy.columnResult)}: </span>
                {#if row.result.kind === "failed"}
                  {wrapDisplayCopy(standingWords.failed)} · {meaning?.result ?? ""}
                {:else}
                  {meaning?.result ?? ""}
                {/if}
              </span>
              <span class="row-duration">
                <span class="visually-hidden">{wrapDisplayCopy(historyPageCopy.columnDuration)}: </span>
                {#if row.span !== null}
                  {ageLabel(row.span.startedAt, now, "duration", row.span.endedAt)}
                {:else}
                  {wrapDisplayCopy(historyPageCopy.durationNotRecorded)}
                {/if}
              </span>
            </a>
          </li>
        {/each}
      </ul>
      {#if showTimestamplessHint}
        <p class="timestampless-hint">{wrapDisplayCopy(historyPageCopy.timestamplessHint)}</p>
      {/if}
    {/if}
  {/if}
</section>

<style>
  .history-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    min-width: 0;
    gap: var(--space-3);
  }

  /* Plain text, not a chip: nothing here is a filter yet, so nothing may look
     like a button that would start one (operator ruling 23.08.). */
  .period-note {
    flex: none;
    font-size: var(--text-xs);
    color: var(--ink-dim);
  }

  .status {
    margin: 0;
    color: var(--ink-dim);
  }

  .failure {
    margin: 0;
    color: var(--signal-failure);
  }

  .history-empty h2 {
    margin: 0;
  }

  .history-head-row {
    display: flex;
    flex-wrap: wrap;
    min-width: 0;
    gap: var(--space-2) var(--space-3);
    padding: 0 var(--space-4);
    font-size: var(--text-2xs);
    letter-spacing: var(--tracking-label);
    text-transform: uppercase;
    color: var(--ink-dim);
  }

  .col-name {
    flex: none;
    width: var(--name-column);
  }

  .col-when {
    flex: none;
    width: var(--when-column);
  }

  .col-purpose {
    flex: none;
    width: var(--purpose-column);
  }

  .col-result {
    flex: 1;
    min-width: 0;
  }

  .col-duration {
    flex: none;
    width: var(--duration-column);
  }

  .history-rows {
    display: grid;
    min-width: 0;
    gap: var(--space-2);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .history-rows li {
    min-width: 0;
  }

  .history-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    min-width: 0;
    gap: var(--space-2) var(--space-3);
    min-height: var(--tap);
    padding: var(--space-3) var(--space-4);
    border: var(--edge) solid var(--line);
    border-radius: var(--r-lg);
    background: var(--panel2);
    color: inherit;
    text-decoration: none;
    box-shadow: var(--shadow);
  }

  .row-name {
    flex: none;
    width: var(--name-column);
    font-weight: var(--weight-strong);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Relative reading, exact local time on hover/title -- the same age/exact
     split every other timestamp on this surface already draws (`when.ts`). */
  .row-when {
    flex: none;
    width: var(--when-column);
    color: var(--ink-dim);
    font-size: var(--text-xs);
    font-variant-numeric: tabular-nums;
  }

  /* Deliberately one line, ellipsis and all: the purpose is named "in one
     line" by design (REQ-UI-13), unlike Result, which must never truncate. */
  .row-purpose {
    flex: none;
    width: var(--purpose-column);
    color: var(--ink-dim);
    font-size: var(--text-sm);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .row-result {
    flex: 1;
    min-width: 0;
    color: var(--ink-dim);
    font-size: var(--text-sm);
    overflow-wrap: anywhere;
  }

  .history-row-failed .row-result {
    color: var(--signal-failure);
  }

  .row-duration {
    flex: none;
    width: var(--duration-column);
    color: var(--ink-dim);
    font-size: var(--text-xs);
    font-variant-numeric: tabular-nums;
  }

  .timestampless-hint {
    margin: 0;
    color: var(--ink-dim);
    font-size: var(--text-xs);
  }

  /**
   * Names each row's fragments (Name/When/Purpose/Result/Duration) for a
   * screen reader without repeating the header aloud for every row --
   * sighted eyes already read the column from `.history-head-row`'s
   * alignment, and duplicating that header once per row would be visual
   * noise rather than a label.
   */
  .visually-hidden {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }

  /* The header keeps naming its columns at every width (operator ruling
     23.08.): a promise a narrow screen hides while the data still sits in
     columns is a geometry the header no longer honestly describes. Duration
     is the one column dropped at this width, so Result -- the fact that must
     never truncate -- gets the room instead (issue #717). Purpose stops
     sharing a line with anything at this width (the wrap below already puts
     it alone), so its truncation width grows to the full row instead of the
     cramped fixed column a wider screen still shares it with. */
  @media (max-width: 32rem) {
    .row-name,
    .col-name {
      width: var(--name-column-narrow);
    }

    .row-purpose,
    .col-purpose {
      flex: 1 1 100%;
      width: auto;
    }

    .row-duration,
    .col-duration {
      display: none;
    }
  }
</style>
