<script lang="ts">
  import { onMount } from "svelte";

  import { isRunV3, type AnyRun, type CockpitApi } from "../api/client";
  import Breadcrumb from "../components/Breadcrumb.svelte";
  import ReadState from "../components/ReadState.svelte";
  import { THE_ONE_PROJECT } from "../lib/project";
  import {
    beginRead,
    confirmRead,
    failRead,
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

  interface ProjectSnapshot {
    runs: AnyRun[];
    workflowNames: ReadonlyMap<string, string>;
  }

  type ProjectReadFailure =
    | { kind: "unavailable"; title: string }
    | { kind: "incomplete"; title: string };

  let project: RetainedRead<ProjectSnapshot, ProjectReadFailure> =
    retainedRead<ProjectSnapshot, ProjectReadFailure>();
  const now = new Date();

  onMount(load);

  async function load(): Promise<void> {
    const begun = beginRead(project);
    project = begun.read;
    try {
      const reading = await readEveryRun((after) => cockpitApi.listRuns(after));
      if (!reading.complete) {
        project = failRead(project, begun.generation, {
          kind: "incomplete",
          title: "Project runs incomplete"
        });
        return;
      }
      const workflowNames = await workflowNamesOf(reading.runs, (hash) =>
        cockpitApi.getWorkflowRevision(hash)
      );
      project = confirmRead(project, begun.generation, {
        runs: reading.runs,
        workflowNames
      });
    } catch {
      project = failRead(project, begun.generation, {
        kind: "unavailable",
        title: "Project runs unavailable"
      });
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

  $: items = newestActivityFirst(project.confirmed?.runs ?? []);
  $: workflowNames = project.confirmed?.workflowNames ?? new Map<string, string>();
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

  <ReadState read={project} label="project runs" onRetry={() => { void load(); }} />

  <section class="queue" aria-labelledby="queue-title">
    <h2 id="queue-title">Queue</h2>
    <p>This project has no priority and no assignment yet.</p>
    <a class="button primary" href="/atelier/new" onclick={(event) => { event.preventDefault(); navigate("/atelier/new"); }}>Start a run</a>
  </section>

  {#if project.confirmed !== null}
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
  {/if}
</section>
