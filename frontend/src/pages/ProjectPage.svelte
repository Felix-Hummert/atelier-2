<script lang="ts">
  import { onMount } from "svelte";

  import {
    isRunV3,
    type AgentConfigurationRevisionListItem,
    type AnyRun,
    CockpitRequestError,
    type CockpitApi,
    type OccupancyRevision,
    type WorkflowRevisionDetail,
    type WorkflowRevisionSummary
  } from "../api/client";
  import Breadcrumb from "../components/Breadcrumb.svelte";
  import ReadState from "../components/ReadState.svelte";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { THE_ONE_PROJECT } from "../lib/project";
  import { projectPageCopy } from "../lib/projectPageCopy";
  import {
    beginRead,
    confirmRead,
    failRead,
    retainedRead,
    type RetainedRead
  } from "../lib/readResource";
  import { runPath } from "../lib/route";
  import { newestActivityFirst, workflowNamesOf } from "../lib/runList";
  import { readEveryAgentConfiguration, readEveryRevision, readEveryRun } from "../lib/runPages";
  import { catalogHeadsOf, catalogNameStateOf, problemCode, type CatalogNameState } from "../lib/catalogName";
  import { namedAgentLabel } from "../lib/namedAgentChoice";
  import { agentRolesOf, groupSavedWorkflows } from "../lib/savedWorkflows";
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

  interface OccupancyEditorSnapshot {
    projectReference: string;
    workflows: WorkflowRevisionSummary[];
    newestByName: Record<string, string>;
    catalogByName: Record<string, CatalogNameState>;
    agents: AgentConfigurationRevisionListItem[];
  }

  interface SelectedOccupancy {
    revision: WorkflowRevisionSummary;
    lineageId: string;
    detail: WorkflowRevisionDetail;
    occupancy: OccupancyRevision | null;
    selections: Record<string, string>;
  }

  type OccupancyEditorFailure = { kind: "unavailable"; title: string };

  let project: RetainedRead<ProjectSnapshot, ProjectReadFailure> =
    retainedRead<ProjectSnapshot, ProjectReadFailure>();
  const now = new Date();

  let occupancyEditor: RetainedRead<OccupancyEditorSnapshot, OccupancyEditorFailure> = retainedRead();
  let occupancySelection: RetainedRead<SelectedOccupancy, OccupancyEditorFailure> = retainedRead();
  let selectedWorkflowHash = "";
  let frozenWrite: {
    projectReference: string;
    lineageId: string;
    input: { revision_number: number; bindings: Array<{ role: string; agent_configuration_revision_hash: string }> };
    body: string;
  } | null = null;
  let writeInFlight = false;
  let saveFailure: "conflict" | "uncertain" | "unavailable" | null = null;
  let saveConfirmed = false;

  onMount(() => { void load(); void loadOccupancyEditor(); });

  function clearOccupancySelection(): void {
    occupancySelection = {
      confirmed: null,
      generation: occupancySelection.generation + 1,
      request: { state: "idle" }
    };
  }

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

  async function loadOccupancyEditor(): Promise<void> {
    if (frozenWrite !== null || selectedWorkflowHash !== "") return;
    const begun = beginRead(occupancyEditor);
    occupancyEditor = begun.read;
    selectedWorkflowHash = "";
    clearOccupancySelection();
    saveFailure = null;
    try {
      const [projects, workflowReading, agentReading] = await Promise.all([
        cockpitApi.listProjects(),
        readEveryRevision((after) => cockpitApi.listWorkflowRevisions(after)),
        readEveryAgentConfiguration((after) => cockpitApi.listAgentConfigurationRevisions(after))
      ]);
      if (projects.items.length !== 1 || !workflowReading.complete || !agentReading.complete) throw new Error("incomplete");
      const names = [...new Set(workflowReading.revisions.flatMap((item) => item.name === null ? [] : [item.name]))];
      const states = Object.fromEntries(await Promise.all(names.map(async (name) => [
        name,
        await catalogNameStateOf(name, (asked) => cockpitApi.getRevisionByName(asked))
      ]))) as Record<string, CatalogNameState>;
      const newestByName = catalogHeadsOf(workflowReading.revisions, states);
      if (newestByName === null) throw new Error("skew");
      const projectResource = projects.items[0];
      if (projectResource === undefined) throw new Error("project missing");
      occupancyEditor = confirmRead(occupancyEditor, begun.generation, {
        projectReference: projectResource.public_project_reference,
        workflows: workflowReading.revisions,
        newestByName,
        catalogByName: states,
        agents: agentReading.configurations
      });
    } catch {
      occupancyEditor = failRead(occupancyEditor, begun.generation, {
        kind: "unavailable", title: "Project occupancy unavailable"
      });
    }
  }

  async function selectOccupancy(revision: WorkflowRevisionSummary | null): Promise<void> {
    if (frozenWrite !== null || occupancyEditor.request.state !== "idle") return;
    selectedWorkflowHash = revision?.workflow_revision_hash ?? "";
    frozenWrite = null;
    saveFailure = null;
    saveConfirmed = false;
    if (revision === null) {
      clearOccupancySelection();
      return;
    }
    const snapshot = occupancyEditor.confirmed;
    if (snapshot === null || revision.name === null) return;
    const state = snapshot.catalogByName[revision.name];
    if (state?.kind !== "admitted" || state.revisionHash !== revision.workflow_revision_hash) {
      selectedWorkflowHash = "";
      clearOccupancySelection();
      return;
    }
    const begun = beginRead(occupancySelection);
    occupancySelection = { ...begun.read, confirmed: null };
    const baseGeneration = occupancyEditor.generation;
    try {
      const [detail, occupancy] = await Promise.all([
        cockpitApi.getWorkflowRevision(revision.workflow_revision_hash),
        cockpitApi.getProjectOccupancy(snapshot.projectReference, state.lineageId).catch((error: unknown) => {
          if (problemCode(error) === "occupancy-missing") return null;
          throw error;
        })
      ]);
      if (
        occupancyEditor.confirmed !== snapshot ||
        occupancyEditor.generation !== baseGeneration ||
        selectedWorkflowHash !== revision.workflow_revision_hash ||
        detail.workflow_revision_hash !== revision.workflow_revision_hash
      ) return;
      const roles = agentRolesOf(detail.graph);
      occupancySelection = confirmRead(occupancySelection, begun.generation, {
        revision, lineageId: state.lineageId, detail, occupancy,
        selections: Object.fromEntries(roles.map((role) => [role, occupancy?.bindings.find((binding) => binding.role === role)?.agent_configuration_revision_hash ?? ""]))
      });
    } catch {
      if (selectedWorkflowHash !== revision.workflow_revision_hash) return;
      occupancySelection = failRead(occupancySelection, begun.generation, {
        kind: "unavailable", title: "Project occupancy unavailable"
      });
    }
  }

  function setOccupancyRole(role: string, value: string): void {
    const selected = occupancySelection.confirmed;
    if (selected === null || frozenWrite !== null) return;
    occupancySelection = { ...occupancySelection, confirmed: { ...selected, selections: { ...selected.selections, [role]: value } } };
    saveFailure = null;
    saveConfirmed = false;
  }

  function occupancyInputOf(selected: SelectedOccupancy) {
    const roles = new Set(agentRolesOf(selected.detail.graph));
    const preserved = (selected.occupancy?.bindings ?? []).filter((binding) => !roles.has(binding.role));
    const authored = [...roles].flatMap((role) => {
      const hash = selected.selections[role] ?? "";
      return hash === "" ? [] : [{ role, agent_configuration_revision_hash: hash }];
    });
    return {
      revision_number: (selected.occupancy?.revision_number ?? 0) + 1,
      bindings: [...preserved, ...authored]
    };
  }

  function sameBindings(left: readonly { role: string; agent_configuration_revision_hash: string }[], right: readonly { role: string; agent_configuration_revision_hash: string }[]): boolean {
    if (left.length !== right.length) return false;
    const rightByRole = new Map(right.map((binding) => [binding.role, binding.agent_configuration_revision_hash]));
    if (rightByRole.size !== right.length) return false;
    return left.every((binding) => rightByRole.get(binding.role) === binding.agent_configuration_revision_hash);
  }

  async function saveOccupancy(retry = false): Promise<void> {
    const snapshot = occupancyEditor.confirmed;
    const selected = occupancySelection.confirmed;
    if (snapshot === null || selected === null || writeInFlight || (frozenWrite !== null && !retry)) return;
    const state = selected.revision.name === null ? null : snapshot.catalogByName[selected.revision.name];
    if (
      occupancyEditor.request.state !== "idle" ||
      state?.kind !== "admitted" ||
      state.revisionHash !== selected.revision.workflow_revision_hash ||
      state.lineageId !== selected.lineageId ||
      !snapshot.workflows.some((item) => item.workflow_revision_hash === selected.revision.workflow_revision_hash)
    ) {
      saveFailure = "unavailable";
      return;
    }
    const currentRevision = selected.occupancy?.revision_number ?? 0;
    if (!Number.isSafeInteger(currentRevision) || currentRevision >= Number.MAX_SAFE_INTEGER) {
      saveFailure = "unavailable";
      return;
    }
    if (retry === false && sameBindings(occupancyInputOf(selected).bindings, selected.occupancy?.bindings ?? [])) {
      return;
    }
    const input = occupancyInputOf(selected);
    const body = JSON.stringify(input);
    const frozen = retry ? frozenWrite : {
      projectReference: snapshot.projectReference,
      lineageId: selected.lineageId,
      input: JSON.parse(body) as typeof input,
      body
    };
    if (frozen === null) return;
    const baseSnapshot = snapshot;
    const selectionGeneration = occupancySelection.generation;
    frozenWrite = frozen;
    writeInFlight = true;
    saveFailure = null;
    try {
      const result = await cockpitApi.putProjectOccupancy(frozen.projectReference, frozen.lineageId, frozen);
      if (
        result.value.public_project_reference !== frozen.projectReference ||
        result.value.lineage_id !== frozen.lineageId ||
        result.value.revision_number !== frozen.input.revision_number ||
        !sameBindings(result.value.bindings, frozen.input.bindings)
      ) throw new Error("identity");
      if (
        occupancyEditor.confirmed !== baseSnapshot ||
        occupancySelection.generation !== selectionGeneration ||
        selectedWorkflowHash !== selected.revision.workflow_revision_hash
      ) {
        frozenWrite = null;
        writeInFlight = false;
        saveFailure = "unavailable";
        return;
      }
      occupancySelection = { ...occupancySelection, confirmed: { ...selected, occupancy: result.value } };
      frozenWrite = null;
      writeInFlight = false;
      saveConfirmed = true;
    } catch (error) {
      writeInFlight = false;
      saveFailure = problemCode(error) === "occupancy-revision-conflict"
        ? "conflict"
        : problemCode(error) === "durable-state-corrupt" || (error instanceof CockpitRequestError && error.definitive_failure)
          ? "unavailable"
          : "uncertain";
    }
  }

  function reloadOccupancy(): void {
    frozenWrite = null;
    saveFailure = null;
    saveConfirmed = false;
    const revision = occupancySelection.confirmed?.revision
      ?? occupancyEditor.confirmed?.workflows.find((item) => item.workflow_revision_hash === selectedWorkflowHash)
      ?? null;
    void selectOccupancy(revision);
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
  $: occupancyRows = groupSavedWorkflows(occupancyEditor.confirmed?.workflows ?? [], occupancyEditor.confirmed?.newestByName ?? {});
  $: selectedRevision = occupancyEditor.confirmed?.workflows.find((item) => item.workflow_revision_hash === selectedWorkflowHash) ?? null;
  $: selectedOccupancy = occupancySelection.confirmed;
  $: selectedRoles = selectedOccupancy === null ? [] : agentRolesOf(selectedOccupancy.detail.graph);
  $: occupancyChanged = selectedOccupancy !== null && !sameBindings(occupancyInputOf(selectedOccupancy).bindings, selectedOccupancy.occupancy?.bindings ?? []);
</script>

<section aria-labelledby="project-title">
  <Breadcrumb steps={[{ label: "Studio", path: "/atelier" }]} current={THE_ONE_PROJECT} {navigate} />

  <header class="page-header">
    <div>
      <p class="eyebrow">{wrapDisplayCopy(projectPageCopy.eyebrow)}</p>
      <h1 id="project-title">{THE_ONE_PROJECT}</h1>
    </div>
    <a class="button primary" href="/atelier/new" aria-label={wrapDisplayCopy(projectPageCopy.startRun)} onclick={(event) => { event.preventDefault(); navigate("/atelier/new"); }}>{wrapDisplayCopy(projectPageCopy.start)}</a>
  </header>

  <ReadState read={project} label="project runs" onRetry={() => { void load(); }} />

  {#if project.confirmed !== null}
    {#if groups.length === 0}
      <p class="muted">{wrapDisplayCopy(projectPageCopy.noRuns)}</p>
    {:else}
      <p id="run-sort" class="muted">{wrapDisplayCopy(projectPageCopy.newestFirst)}</p>
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
                <span class={`state-label state-${group.standing}`}><span aria-hidden="true">{standingMarks[group.standing]}</span>{standingWords[group.standing]}</span>
                {#if group.standing === "waiting"}
                  <span class="state-label state-waiting">{humanMove(run.state)}</span>
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

  <section class="queue" aria-labelledby="queue-title">
    <h2 id="queue-title">{wrapDisplayCopy(projectPageCopy.queueTitle)}</h2>
    <p>{wrapDisplayCopy(projectPageCopy.queueAbsence)}</p>
  </section>

  <section class="occupancy-editor" aria-labelledby="occupancy-title">
    <p class="eyebrow">{wrapDisplayCopy(projectPageCopy.occupancyEyebrow)}</p>
    <h2 id="occupancy-title">{wrapDisplayCopy(projectPageCopy.occupancyTitle)}</h2>
    {#if frozenWrite === null}
      {#if selectedWorkflowHash === ""}
        <ReadState read={occupancyEditor} label="project occupancy" onRetry={() => { void loadOccupancyEditor(); }} />
      {/if}
    {:else if writeInFlight}
      <p class="muted" role="status">Saving occupancy…</p>
    {/if}
    {#if occupancyEditor.confirmed !== null}
      {#if occupancyRows.length === 0}
        <p class="muted">No admitted workflows yet.</p>
      {:else}
        <label class="named-agent">Workflow
          <select
            aria-label="Workflow occupancy"
            disabled={frozenWrite !== null || occupancyEditor.request.state !== "idle"}
            value={selectedWorkflowHash}
            onchange={(event) => {
              const selector = event.currentTarget;
              const revision = occupancyEditor.confirmed?.workflows.find((item) => item.workflow_revision_hash === event.currentTarget.value);
              void selectOccupancy(revision ?? null).then(() => { selector.value = selectedWorkflowHash; });
            }}
          >
            <option value="">Choose</option>
            {#each occupancyRows as row (row.key)}
              {@const revision = row.revisions[0]}
              {#if revision !== undefined && revision.name !== null && occupancyEditor.confirmed.catalogByName[revision.name]?.kind === "admitted"}
                <option value={revision.workflow_revision_hash}>{revision.name}</option>
              {/if}
            {/each}
          </select>
        </label>
      {/if}
      {#if occupancySelection.request.state !== "idle"}
        <ReadState
          read={occupancySelection}
          label="selected project occupancy"
          onRetry={() => { void selectOccupancy(selectedRevision); }}
        />
      {/if}
      {#if selectedOccupancy !== null}
        {#if selectedRoles.length === 0}
          <p class="muted">This workflow declares no agent roles.</p>
        {:else}
          {#if selectedOccupancy.occupancy === null || selectedOccupancy.occupancy.bindings.length === 0}
            <p class="muted">No project recommendations yet.</p>
          {/if}
          {#if occupancyEditor.confirmed.agents.length === 0}
            <p class="muted">No published agents yet.</p>
          {/if}
          <div class="binding-list">
            {#each selectedRoles as role (role)}
              <label class="named-agent">{role}
                <select
                  value={selectedOccupancy.selections[role] ?? ""}
                  aria-label={`Recommendation for ${role}`}
                  disabled={frozenWrite !== null}
                  onchange={(event) => setOccupancyRole(role, event.currentTarget.value)}
                >
                  <option value="">None</option>
                  {#if selectedOccupancy.selections[role] && !occupancyEditor.confirmed.agents.some((agent) => agent.agent_configuration_revision_hash === selectedOccupancy?.selections[role])}
                    <option value={selectedOccupancy.selections[role]} disabled>Unavailable</option>
                  {/if}
                  {#each occupancyEditor.confirmed.agents as agent (agent.agent_configuration_revision_hash)}
                    <option value={agent.agent_configuration_revision_hash}>{namedAgentLabel(agent)}</option>
                  {/each}
                </select>
              </label>
            {/each}
          </div>
          {#if saveConfirmed}
            <p class="occupancy-confirmed"><span aria-hidden="true">✓</span> Saved</p>
          {/if}
          {#if saveFailure === "conflict"}
            <div class="inbox-card" role="alert"><span class="inbox-mark" aria-hidden="true">◇</span><strong>Occupancy changed elsewhere.</strong></div>
            <button type="button" onclick={reloadOccupancy}>Reload</button>
          {:else if saveFailure === "uncertain" && frozenWrite !== null}
            <div class="inbox-card" role="alert"><span class="inbox-mark" aria-hidden="true">◇</span><strong>Occupancy save unconfirmed.</strong></div>
            <button type="button" onclick={() => { void saveOccupancy(true); }}>Retry</button>
          {:else if saveFailure === "unavailable"}
            <div class="inbox-card" role="alert"><span class="inbox-mark" aria-hidden="true">◇</span><strong>Project occupancy unavailable.</strong></div>
            <button type="button" onclick={reloadOccupancy}>Reload</button>
          {:else}
            <button class="primary" type="button" disabled={frozenWrite !== null || !occupancyChanged} onclick={() => { void saveOccupancy(); }}>Save</button>
          {/if}
        {/if}
      {/if}
    {/if}
  </section>

</section>
