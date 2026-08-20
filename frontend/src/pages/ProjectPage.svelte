<script lang="ts">
  import { onMount } from "svelte";

  import { isRunV3, type AnyRun, type CockpitApi, type RunPage } from "../api/client";
  import Breadcrumb from "../components/Breadcrumb.svelte";
  import ProblemNotice from "../components/ProblemNotice.svelte";
  import { THE_ONE_PROJECT } from "../lib/project";
  import {
    beginRead,
    confirmRead,
    retainedRead,
    type RetainedRead
  } from "../lib/readResource";
  import { runPath } from "../lib/route";
  import { newestActivityFirst, workflowNamesOf } from "../lib/runList";
  import { readEveryRun } from "../lib/runPages";
  import { humanMove, runsStanding, standingMarks, standingOrder, standingWords } from "../lib/runState";
  import { ageLabel, exactLocal } from "../lib/when";

  export let cockpitApi: CockpitApi;
  export let navigate: (path: string) => void;

  let runs: RetainedRead<RunPage, never> = retainedRead<RunPage, never>();
  let workflowNames: ReadonlyMap<string, string> = new Map();
  let failureMessage: string | null = null;
  const now = new Date();

  onMount(load);

  async function load(): Promise<void> {
    const begun = beginRead(runs);
    runs = begun.read;
    failureMessage = null;
    try {
      const reading = await readEveryRun((after) => cockpitApi.listRuns(after));
      workflowNames = await workflowNamesOf(reading.runs, (hash) =>
        cockpitApi.getWorkflowRevision(hash)
      );
      runs = confirmRead(runs, begun.generation, { items: reading.runs, next_after: null });
      if (!reading.complete) {
        failureMessage = `Some of this project could not be read, so what is below is incomplete: ${reading.unreadable}.`;
      }
    } catch (error) {
      failureMessage = error instanceof Error ? error.message : "This project could not be read.";
      runs = { ...runs, request: { state: "idle" } };
    }
  }

  function listedWorkflowName(
    run: AnyRun,
    names: ReadonlyMap<string, string>
  ): string | null {
    if (!isRunV3(run)) return null;
    return names.get(run.workflow_revision_hash) ?? null;
  }

  function listedWhen(
    run: AnyRun
  ): { datetime: string; exact: string; age: string } | null {
    if (!isRunV3(run) || run.started_at == null) return null;
    const ended = run.ended_at ?? null;
    return {
      datetime: run.started_at,
      exact:
        ended === null
          ? exactLocal(run.started_at)
          : `${exactLocal(run.started_at)} → ${exactLocal(ended)}`,
      age: ageLabel(
        run.started_at,
        now,
        ended === null ? "for" : "ago",
        ended === null ? undefined : ended
      )
    };
  }

  $: items = newestActivityFirst(runs.confirmed?.items ?? []);
  $: groups = standingOrder
    .map((standing) => ({ standing, runs: runsStanding(items, standing) }))
    .filter((group) => group.runs.length > 0);
</script>

<section aria-labelledby="project-title">
  <Breadcrumb steps={[{ label: "Studio", path: "/atelier" }]} current={THE_ONE_PROJECT} {navigate} />

  <header class="page-header">
    <div>
      <p class="eyebrow">Project</p>
      <h1 id="project-title">{THE_ONE_PROJECT}</h1>
    </div>
  </header>

  <div class="toolbar">
    <button class="quiet" type="button" onclick={load}>Refresh</button>
  </div>

  {#if failureMessage !== null}
    <ProblemNotice message={failureMessage} />
  {/if}

  <section class="queue" aria-labelledby="queue-title">
    <h2 id="queue-title">Queue</h2>
    <p>This project has no priority and no assignment yet.</p>
    <a class="button primary" href="/atelier/new" onclick={(event) => { event.preventDefault(); navigate("/atelier/new"); }}>Start a run</a>
  </section>

  {#if runs.confirmed !== null}
    {#if groups.length === 0}
      <p class="muted">No runs here yet.</p>
    {:else}
      <p id="run-sort" class="muted">Newest first.</p>
    {/if}
    {#each groups as group (group.standing)}
      <section class="run-group" aria-labelledby={`group-${group.standing}`} aria-describedby="run-sort">
        <h2 class="section-title" id={`group-${group.standing}`}>{standingWords[group.standing]}</h2>
        <ul class="card-list">
          {#each group.runs as run (run.public_run_reference)}
            {@const workflowName = listedWorkflowName(run, workflowNames)}
            {@const when = listedWhen(run)}
            <li>
              <a class="run-card" href={runPath(run.public_run_reference)} onclick={(event) => { event.preventDefault(); navigate(runPath(run.public_run_reference)); }}>
                <div class="run-card-main">
                  <strong>{run.run_id}</strong>
                  <span class="run-card-assignment">
                    {workflowName === null ? THE_ONE_PROJECT : `${THE_ONE_PROJECT} · ${workflowName}`}
                  </span>
                </div>
                {#if group.standing === "waiting"}
                  <span class="state-label state-waiting"><span aria-hidden="true">{standingMarks.waiting}</span>{humanMove(run.state)}</span>
                {/if}
                {#if when !== null}
                  <span class="run-card-when">
                    <time datetime={when.datetime}>{when.exact}</time>
                    <span>{when.age}</span>
                  </span>
                {/if}
              </a>
            </li>
          {/each}
        </ul>
      </section>
    {/each}
  {:else if runs.request.state === "loading"}
    <p class="status" role="status">Looking…</p>
  {/if}
</section>
