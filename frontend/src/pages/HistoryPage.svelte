<script lang="ts">
  import { onMount } from "svelte";

  import type { AnyRun, CockpitApi } from "../api/client";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import {
    HISTORY_PERIOD_DAYS,
    hasTimestamplessRows,
    projectHistoryRows,
    withinHistoryPeriod
  } from "../lib/historyRows";
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
  import { workflowNamesOf } from "../lib/runList";
  import { readEveryRun } from "../lib/runPages";
  import { ageLabel } from "../lib/when";

  export let cockpitApi: CockpitApi;
  export let navigate: (path: string) => void;

  interface HistorySnapshot {
    runs: AnyRun[];
    workflowNames: ReadonlyMap<string, string>;
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
      const workflowNames = await workflowNamesOf(runs, (hash) =>
        cockpitApi.getWorkflowRevision(hash)
      );
      history = confirmRead(history, begun.generation, { runs, workflowNames });
    } catch {
      history = failRead(history, begun.generation, {
        kind: "unavailable",
        title: historyPageCopy.listUnavailable
      });
    }
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
</script>

<section class="history-page surface" aria-labelledby="history-title">
  <header class="surface-head history-head">
    <div>
      <p class="eyebrow">{wrapDisplayCopy(historyPageCopy.eyebrow)}</p>
      <h1 id="history-title">{wrapDisplayCopy(historyPageCopy.title)}</h1>
    </div>
    <span class="period-chip">{periodChipLabel(HISTORY_PERIOD_DAYS)}</span>
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
        <span class="col-result">{wrapDisplayCopy(historyPageCopy.columnResult)}</span>
        <span class="col-duration">{wrapDisplayCopy(historyPageCopy.columnDuration)}</span>
      </div>
      <ul class="history-rows">
        {#each visibleRows as row (row.run.public_run_reference)}
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
              <span class="row-result">
                <span class="visually-hidden">{wrapDisplayCopy(historyPageCopy.columnResult)}: </span>
                {#if row.result.kind === "failed"}
                  {wrapDisplayCopy(standingWords.failed)} · {row.result.nodeId}
                {:else}
                  {wrapDisplayCopy(standingWords.done)}
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

  .period-chip {
    flex: none;
    border: 1px solid var(--line);
    border-radius: var(--r-pill);
    padding: 0.15rem 0.7rem;
    font-size: var(--text-xs);
    color: var(--muted);
    background: var(--panel2);
  }

  .status {
    margin: 0;
    color: var(--muted);
  }

  .failure {
    margin: 0;
    color: var(--danger);
  }

  .history-empty h2 {
    margin: 0;
  }

  .history-head-row {
    display: flex;
    min-width: 0;
    gap: var(--space-2) var(--space-3);
    padding: 0 var(--space-4);
    font-size: var(--text-2xs);
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--muted);
  }

  .col-name {
    flex: none;
    width: 16rem;
  }

  .col-result {
    flex: 1;
    min-width: 0;
  }

  .col-duration {
    flex: none;
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
    min-height: 44px;
    padding: var(--space-3) var(--space-4);
    border: 1px solid var(--line);
    border-radius: var(--r-lg);
    background: var(--panel2);
    color: inherit;
    text-decoration: none;
    box-shadow: var(--shadow);
  }

  .row-name {
    flex: none;
    font-weight: 650;
    max-width: 16rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .row-result {
    flex: 1;
    min-width: 0;
    color: var(--muted);
    font-size: var(--text-sm);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .history-row-failed .row-result {
    color: var(--danger);
  }

  .row-duration {
    flex: none;
    color: var(--muted);
    font-size: var(--text-xs);
    font-variant-numeric: tabular-nums;
  }

  .timestampless-hint {
    margin: 0;
    color: var(--muted);
    font-size: var(--text-xs);
  }

  /**
   * Names each row's three fragments (Name/Result/Took) for a screen reader
   * without repeating the header aloud for every row -- sighted eyes already
   * read the column from `.history-head-row`'s alignment, and duplicating
   * that header once per row would be visual noise rather than a label.
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

  @media (max-width: 32rem) {
    .row-name {
      max-width: 8rem;
    }

    .col-name {
      width: 8rem;
    }

    .history-head-row {
      display: none;
    }
  }
</style>
