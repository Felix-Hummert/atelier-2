<script lang="ts">
  import { onMount } from "svelte";

  import type { AnyRun, CockpitApi, NodeDetail } from "../api/client";
  import { connectionState, onConnectionRecovered } from "../lib/connectionState";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { decodeUtf8Base64 } from "../lib/exactBytes";
  import { shortPublicRunReference } from "../lib/fingerprint";
  import {
    HISTORY_PERIOD_DAYS,
    hasTimestamplessRows,
    historyResultNodeId,
    historyWhenLabel,
    historyWorkItemLabel,
    projectHistoryRows,
    withinHistoryPeriod,
    type HistoryRow,
    type HistoryRowExtras,
    type HistoryWhenDay,
    type HistoryWorkItem
  } from "../lib/historyRows";
  import { historyOutcome } from "../lib/historyOutcome";
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
  import {
    trackerItemHref,
    workItemReferenceFromJob,
    type TrackerSourceConnection
  } from "../lib/trackerItem";
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
  let sourceConnection: TrackerSourceConnection | null = null;

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
      const sourcePromise = loadSource();
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
      sourceConnection = await sourcePromise;
      if (history.generation !== begun.generation) return;
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

  async function loadSource(): Promise<TrackerSourceConnection | null> {
    try {
      const projects = await cockpitApi.listProjects();
      const reference = projects.items[0]?.public_project_reference;
      if (reference === undefined) return null;
      const connection = await cockpitApi.getProjectSourceConnection(reference);
      return {
        source_kind: connection.source_kind,
        source_address: connection.source_address
      };
    } catch {
      return null;
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
        workItem: workItemFromDetail(detail),
        resultSentence: resultSentenceFromDetail(row.workflowName, row.result.kind, detail)
      };
    } catch {
      return { workItem: null, resultSentence: null };
    }
  }

  function workItemFromDetail(detail: NodeDetail | null | undefined): HistoryWorkItem | null {
    if (detail == null || detail.job_base64 == null || detail.job_base64.length === 0) {
      return null;
    }
    const job = decodeUtf8Base64(detail.job_base64);
    if (job == null) return null;
    const reference = workItemReferenceFromJob(job);
    if (reference === null) return null;
    return {
      reference,
      title: null,
      href: trackerItemHref(reference, sourceConnection)
    };
  }

  function resultSentenceFromDetail(
    workflowName: string,
    kind: HistoryRow["result"]["kind"],
    detail: NodeDetail | null | undefined
  ): string | null {
    if (detail == null) return null;
    const encoded =
      kind === "failed" ? detail.refusal_output?.value_base64 : detail.answer?.value_base64;
    if (encoded == null || encoded.length === 0) return null;
    const decoded = decodeUtf8Base64(encoded);
    if (decoded == null || decoded.length === 0) return null;
    return historyOutcome(workflowName, decoded);
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
    if (sentence !== null) return `${standing} — ${wrapDisplayCopy(sentence)}`;
    return `${standing} · ${nodeId}`;
  }

  function open(publicReference: string) {
    return (event: Event) => {
      event.preventDefault();
      navigate(runPath(publicReference));
    };
  }

  function runLinkName(row: HistoryRow): string {
    const name = row.purpose !== null ? `${row.purpose} ${row.workflowName}` : row.workflowName;
    return `${name} ${shortPublicRunReference(row.run.public_run_reference)}`;
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
            <div
              class="history-row history-row-{row.result.kind}"
              class:history-row-no-work-item={row.workItem === null}
            >
              <a
                class="history-row-open"
                href={runPath(row.run.public_run_reference)}
                onclick={open(row.run.public_run_reference)}
                title={row.activityAt === null ? undefined : exactLocal(row.activityAt)}
              >
                <span class="visually-hidden">{runLinkName(row)}</span>
              </a>
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
                  {#if row.workItem.href !== null}
                    <a
                      class="row-work-item-link"
                      href={row.workItem.href}
                      onclick={(event) => event.stopPropagation()}
                    >{historyWorkItemLabel(row.workItem)}</a>
                  {:else}
                    {historyWorkItemLabel(row.workItem)}
                  {/if}
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
                    {wrapDisplayCopy(row.result.sentence)}
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
              <span class="row-run" aria-hidden="true">{shortPublicRunReference(row.run.public_run_reference)}</span>
            </div>
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
    position: relative;
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
    box-shadow: var(--shadow);
  }

  .history-row-open {
    position: absolute;
    inset: 0;
    z-index: 1;
    border-radius: inherit;
  }

  .row-name {
    position: relative;
    z-index: 0;
    display: flex;
    flex: none;
    flex-direction: column;
    width: var(--name-column);
    min-width: 0;
    pointer-events: none;
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
    position: relative;
    z-index: 0;
    color: var(--ink-dim);
    font-size: var(--text-xs);
    font-variant-numeric: tabular-nums;
    pointer-events: none;
    white-space: nowrap;
  }

  /* Never an ellipsis (REQ-UI-13): a result sentence can run long, so the
     cell wraps and is clamped to two lines instead of being cut mid-word; the
     run page shows the sentence in full. */
  .row-result {
    display: -webkit-box;
    position: relative;
    z-index: 0;
    flex: 1;
    min-width: 0;
    overflow: hidden;
    color: var(--ink-dim);
    font-size: var(--text-sm);
    pointer-events: none;
    overflow-wrap: anywhere;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    line-clamp: 2;
  }

  .history-row-failed .row-result {
    color: var(--signal-failure);
  }

  .row-work-item {
    position: relative;
    z-index: 2;
    flex: none;
    width: var(--work-item-column);
    overflow: hidden;
    color: var(--ink-dim);
    font-size: var(--text-xs);
    pointer-events: none;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .row-work-item-link {
    pointer-events: auto;
    color: inherit;
    text-decoration: none;
  }

  .row-work-item-link:hover,
  .row-work-item-link:focus-visible {
    text-decoration: underline;
  }

  .row-duration {
    position: relative;
    z-index: 0;
    flex: none;
    width: var(--duration-column);
    color: var(--ink-dim);
    font-size: var(--text-xs);
    pointer-events: none;
    font-variant-numeric: tabular-nums;
  }

  /* Dim trailing token: the run's public reference, shortened the way the
     run view shortens hashes. Not a new column — the row already leads to
     its run. */
  .row-run {
    position: relative;
    z-index: 0;
    flex: none;
    overflow: hidden;
    color: var(--ink-faint);
    font-size: var(--text-2xs);
    font-variant-numeric: tabular-nums;
    pointer-events: none;
    white-space: nowrap;
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
     23.08.). Duration drops at this width. Work item drops only when it is
     the placeholder; a filled cell stays. The row stacks so When, work
     item and Result each keep a readable line instead of Result collapsing
     to a glyph (mockup v8 §05 at 390). */
  @media (max-width: 32rem) {
    .history-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto;
      align-items: start;
    }

    .row-name {
      grid-column: 1;
      grid-row: 1;
      width: auto;
      min-width: 0;
    }

    .col-name {
      width: var(--name-column-narrow);
    }

    .row-when {
      grid-column: 2;
      grid-row: 1;
      width: auto;
      text-align: right;
    }

    .row-run {
      grid-column: 3;
      grid-row: 1;
    }

    .row-work-item {
      grid-column: 1 / -1;
      grid-row: 2;
      width: auto;
    }

    .row-result {
      grid-column: 1 / -1;
      grid-row: 3;
      flex: none;
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
