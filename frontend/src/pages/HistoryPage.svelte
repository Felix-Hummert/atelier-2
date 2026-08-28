<script lang="ts">
  import { onMount } from "svelte";

  import type { AnyRun, CockpitApi, NodeDetail } from "../api/client";
  import { connectionState, onConnectionRecovered } from "../lib/connectionState";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { decodeUtf8Base64 } from "../lib/exactBytes";
  import {
    HISTORY_PERIOD_DAYS,
    hasTimestamplessRows,
    historyResultNodeId,
    historyResultSentence,
    historyWhenLabel,
    projectHistoryRows,
    withinHistoryPeriod,
    type HistoryRow,
    type HistoryRowExtras,
    type HistoryWhenDay
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
  import { ageLabel, exactLocal } from "../lib/when";
  import { WORKSHOP_DESTINATION } from "../lib/workshop";

  export let cockpitApi: CockpitApi;
  export let navigate: (path: string) => void;

  const catalogPath = WORKSHOP_DESTINATION.catalog.path;

  interface HistorySnapshot {
    runs: AnyRun[];
    workflowNames: ReadonlyMap<string, string>;
  }

  type HistoryReadFailure =
    | { kind: "unavailable"; title: string }
    | { kind: "incomplete"; title: string };

  type ExtrasLoad =
    | { status: "loading" }
    | { status: "settled"; extras: HistoryRowExtras };

  let history: RetainedRead<HistorySnapshot, HistoryReadFailure> =
    retainedRead<HistorySnapshot, HistoryReadFailure>();
  const now = new Date();
  let extrasLoadByReference: Map<string, ExtrasLoad> = new Map();
  let extrasToken = 0;

  onMount(() => {
    void load();
    // A read that failed while the connection was lost is worth asking again
    // on its own once it returns, with no reload (#700).
    return onConnectionRecovered(() => { void load(); });
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
      if (history.generation !== begun.generation) return;
      const visible = projectHistoryRows(runs, workflowNames).filter((row) =>
        withinHistoryPeriod(row, now)
      );
      void settleExtras(visible);
    } catch {
      history = failRead(history, begun.generation, {
        kind: "unavailable",
        title: historyPageCopy.listUnavailable
      });
    }
  }

  async function settleExtras(rows: HistoryRow[]): Promise<void> {
    const token = ++extrasToken;
    extrasLoadByReference = new Map(
      rows.map((row) => [row.run.public_run_reference, { status: "loading" as const }])
    );
    const settledEntries = await Promise.all(
      rows.map(async (row) => {
        const extras = await extrasForRow(row);
        return [row.run.public_run_reference, extras] as const;
      })
    );
    if (token !== extrasToken) return;
    extrasLoadByReference = new Map(
      settledEntries.map(([reference, extras]) => [
        reference,
        { status: "settled" as const, extras }
      ])
    );
  }

  async function extrasForRow(row: HistoryRow): Promise<HistoryRowExtras> {
    try {
      const detail = await cockpitApi.getNodeDetail(
        row.run.public_run_reference,
        historyResultNodeId(row.run)
      );
      return {
        workItem: null,
        resultSentence: resultSentenceFromDetail(row.result.kind, detail)
      };
    } catch {
      return { workItem: null, resultSentence: null };
    }
  }

  function resultSentenceFromDetail(
    kind: HistoryRow["result"]["kind"],
    detail: NodeDetail | null | undefined
  ): string | null {
    if (detail == null) return null;
    if (kind === "failed") {
      const output = detail.refusal_output?.value_base64;
      if (output != null && output.length > 0) {
        const decoded = decodeUtf8Base64(output);
        if (decoded != null) {
          const sentence = historyResultSentence(decoded);
          if (sentence.length > 0) return sentence;
        }
      }
      if (detail.refusal != null && detail.refusal.length > 0) {
        const sentence = historyResultSentence(detail.refusal);
        return sentence.length === 0 ? null : sentence;
      }
      return null;
    }
    const value = detail.answer?.value_base64;
    if (value == null || value.length === 0) return null;
    const decoded = decodeUtf8Base64(value);
    if (decoded == null) return null;
    const sentence = historyResultSentence(decoded);
    return sentence.length === 0 ? null : sentence;
  }

  function settledExtrasByReference(
    loads: ReadonlyMap<string, ExtrasLoad>
  ): ReadonlyMap<string, HistoryRowExtras> {
    return new Map(
      [...loads].flatMap(([reference, load]) =>
        load.status === "settled" ? [[reference, load.extras] as const] : []
      )
    );
  }

  function whenDayText(day: HistoryWhenDay): string {
    if (day.kind === "today") return wrapDisplayCopy(historyPageCopy.today);
    if (day.kind === "yesterday") return wrapDisplayCopy(historyPageCopy.yesterday);
    return day.weekday;
  }

  function failedResultCopy(nodeId: string, sentence: string | null, settled: boolean): string {
    const standing = wrapDisplayCopy(standingWords.failed);
    if (!settled) return standing;
    if (sentence !== null) return `${standing} — ${sentence}`;
    return `${standing} · ${nodeId}`;
  }

  function open(publicReference: string) {
    return (event: Event) => {
      event.preventDefault();
      navigate(runPath(publicReference));
    };
  }

  $: extrasByReference = settledExtrasByReference(extrasLoadByReference);
  $: rows = history.confirmed === null
    ? []
    : projectHistoryRows(history.confirmed.runs, history.confirmed.workflowNames, extrasByReference);
  $: visibleRows = rows.filter((row) => withinHistoryPeriod(row, now));
  $: showTimestamplessHint = hasTimestamplessRows(visibleRows);
  $: hasWorkItem = visibleRows.some((row) => row.workItem !== null);
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
  {:else if history.request.state === "failed" && $connectionState !== "reconnecting"}
    <!-- The central connection line above already names an unreachable
         workshop once; this page's own "unavailable" stays quiet for that
         one case rather than repeating it, and reads again on its own once
         the connection returns (#700). -->
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
          href={catalogPath}
          onclick={(event) => { event.preventDefault(); navigate(catalogPath); }}
        >{wrapDisplayCopy(historyPageCopy.emptyNext)}</a>
      </div>
    {:else}
      <div
        class="history-head-row"
        class:history-head-no-work-item={!hasWorkItem}
        aria-hidden="true"
      >
        <span class="col-when">{wrapDisplayCopy(historyPageCopy.columnWhen)}</span>
        <span class="col-name">{wrapDisplayCopy(historyPageCopy.columnName)}</span>
        <span class="col-work-item">{wrapDisplayCopy(historyPageCopy.columnWorkItem)}</span>
        <span class="col-result">{wrapDisplayCopy(historyPageCopy.columnResult)}</span>
        <span class="col-duration">{wrapDisplayCopy(historyPageCopy.columnDuration)}</span>
      </div>
      <ul class="history-rows">
        {#each visibleRows as row (row.run.public_run_reference)}
          {@const extras = extrasLoadByReference.get(row.run.public_run_reference)}
          <li>
            <a
              class="history-row history-row-{row.result.kind}"
              class:history-row-no-work-item={row.workItem === null}
              href={runPath(row.run.public_run_reference)}
              onclick={open(row.run.public_run_reference)}
            >
              <span class="row-when">
                <span class="visually-hidden">{wrapDisplayCopy(historyPageCopy.columnWhen)}: </span>
                {#if row.activityAt !== null}
                  {@const when = historyWhenLabel(row.activityAt, now)}
                  <time datetime={row.activityAt} title={exactLocal(row.activityAt)}>
                    {whenDayText(when.day)} {when.clock}
                  </time>
                {:else}
                  {wrapDisplayCopy(historyPageCopy.notRecorded)}
                {/if}
              </span>
              <span class="row-name">
                <span class="visually-hidden">{wrapDisplayCopy(historyPageCopy.columnName)}: </span>
                <span class="row-purpose">{row.purpose ?? row.workflowName}</span>
                {#if row.purpose !== null}
                  <small class="row-workflow">{row.workflowName}</small>
                {/if}
              </span>
              <span class="row-work-item">
                <span class="visually-hidden">{wrapDisplayCopy(historyPageCopy.columnWorkItem)}: </span>
                {#if row.workItem !== null}
                  {row.workItem}
                {:else}
                  {wrapDisplayCopy(historyPageCopy.workItemPlaceholder)}
                {/if}
              </span>
              <span class="row-result">
                <span class="visually-hidden">{wrapDisplayCopy(historyPageCopy.columnResult)}: </span>
                {#if row.result.kind === "failed"}
                  {failedResultCopy(row.result.nodeId, row.result.sentence, extras?.status === "settled")}
                {:else if extras?.status === "settled"}
                  {#if row.result.sentence !== null}
                    {row.result.sentence}
                  {:else}
                    {wrapDisplayCopy(historyPageCopy.notRecorded)}
                  {/if}
                {/if}
              </span>
              <span class="row-duration">
                <span class="visually-hidden">{wrapDisplayCopy(historyPageCopy.columnDuration)}: </span>
                {#if row.span !== null}
                  {ageLabel(row.span.startedAt, now, "duration", row.span.endedAt)}
                {:else}
                  {wrapDisplayCopy(historyPageCopy.notRecorded)}
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

  .col-when,
  .row-when {
    flex: none;
    width: var(--when-column);
  }

  .col-work-item {
    flex: none;
    width: var(--work-item-column);
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
    display: flex;
    flex: none;
    flex-direction: column;
    width: var(--name-column);
    min-width: 0;
  }

  /* The purpose line (mockup v8 §05: "Purpose (the order sentence)"); it can
     be as terse as one order's own name today (#717's honest first slice), so
     it stays on one line rather than wrapping. */
  .row-purpose {
    overflow: hidden;
    font-weight: var(--weight-strong);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* "…, workflow small beneath" (mockup v8 §05): the recipe name, dim and
     smaller, shown only when the purpose line above says something the
     workflow name does not already say on its own. */
  .row-workflow {
    overflow: hidden;
    font-size: var(--text-xs);
    color: var(--ink-dim);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Calendar-clock fragments, exact local time on hover/title. */
  .row-when {
    color: var(--ink-dim);
    font-size: var(--text-xs);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  /* Never an ellipsis (REQ-UI-13): a result sentence can run long, so the
     cell wraps and is clamped to two lines instead of being cut mid-word; the
     run page shows the sentence in full. */
  .row-result {
    display: -webkit-box;
    flex: 1;
    min-width: 0;
    overflow: hidden;
    color: var(--ink-dim);
    font-size: var(--text-sm);
    overflow-wrap: anywhere;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    line-clamp: 2;
  }

  .history-row-failed .row-result {
    color: var(--signal-failure);
  }

  .row-work-item {
    flex: none;
    width: var(--work-item-column);
    overflow: hidden;
    color: var(--ink-dim);
    font-size: var(--text-xs);
    text-overflow: ellipsis;
    white-space: nowrap;
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
   * Names each row's fragments (When/Purpose/Work item/Result/Duration) for a
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
     23.08.). Duration drops at this width so Result -- the fact that must
     never truncate -- gets the room. Work item drops only when it is the
     placeholder; a filled cell stays. */
  @media (max-width: 32rem) {
    .row-name {
      flex: 1 1 auto;
      width: auto;
      min-width: 0;
    }

    .col-name {
      width: var(--name-column-narrow);
    }

    .row-duration,
    .col-duration {
      display: none;
    }

    .history-row-no-work-item .row-work-item,
    .history-head-no-work-item .col-work-item {
      display: none;
    }
  }
</style>
