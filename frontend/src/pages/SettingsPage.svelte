<script lang="ts">
  import { onMount } from "svelte";

  import {
    type AgentConfigurationRevisionListItem,
    type CockpitApi,
    type ProjectSourceConnectionRevision
  } from "../api/client";
  import ReadState from "../components/ReadState.svelte";
  import { problemCode } from "../lib/catalogName";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { THE_ONE_PROJECT } from "../lib/project";
  import {
    beginRead,
    confirmRead,
    failRead,
    retainedRead,
    type RetainedRead
  } from "../lib/readResource";
  import { readEveryAgentConfiguration } from "../lib/runPages";
  import { settingsPageCopy } from "../lib/settingsPageCopy";

  export let cockpitApi: CockpitApi;

  type SourcesRead = ProjectSourceConnectionRevision | null;
  type SettingsFailure = { kind: "unavailable"; title: string };

  let sources: RetainedRead<SourcesRead, SettingsFailure> = retainedRead();
  let models: RetainedRead<AgentConfigurationRevisionListItem[], SettingsFailure> =
    retainedRead();

  onMount(() => { void load(); });

  async function load(): Promise<void> {
    const sourcesBegun = beginRead(sources);
    const modelsBegun = beginRead(models);
    sources = sourcesBegun.read;
    models = modelsBegun.read;
    try {
      const projects = await cockpitApi.listProjects();
      const project = projects.items[0];
      if (project === undefined) throw new Error("served project missing");
      try {
        sources = confirmRead(
          sources,
          sourcesBegun.generation,
          await cockpitApi.getProjectSourceConnection(project.public_project_reference)
        );
      } catch (error) {
        if (problemCode(error) === "project-source-not-connected") {
          sources = confirmRead(sources, sourcesBegun.generation, null);
        } else {
          sources = failRead(sources, sourcesBegun.generation, {
            kind: "unavailable",
            title: settingsPageCopy.sourcesUnavailable
          });
        }
      }
      const reading = await readEveryAgentConfiguration((after) =>
        cockpitApi.listAgentConfigurationRevisions(after)
      );
      if (!reading.complete) throw new Error("model page incomplete");
      models = confirmRead(models, modelsBegun.generation, reading.configurations);
    } catch {
      if (sources.request.state === "loading") {
        sources = failRead(sources, sourcesBegun.generation, {
          kind: "unavailable",
          title: settingsPageCopy.sourcesUnavailable
        });
      }
      models = failRead(models, modelsBegun.generation, {
        kind: "unavailable",
        title: settingsPageCopy.modelsUnavailable
      });
    }
  }
</script>

<section class="settings-page" aria-labelledby="settings-title">
  <header>
    <h1 id="settings-title">{THE_ONE_PROJECT}</h1>
  </header>

  <section aria-labelledby="sources-title">
    <h2 id="sources-title">{wrapDisplayCopy(settingsPageCopy.sourcesTitle)}</h2>
    <ReadState read={sources} label={settingsPageCopy.sourcesLabel} onRetry={() => { void load(); }} />
    {#if sources.confirmed === null && sources.request.state === "idle"}
      <p class="muted">{wrapDisplayCopy(settingsPageCopy.sourcesEmpty)}</p>
    {:else if sources.confirmed !== null}
      <dl class="source-list">
        <div><dt>{wrapDisplayCopy(settingsPageCopy.sourceKind)}</dt><dd>{sources.confirmed.source_kind}</dd></div>
        <div><dt>{wrapDisplayCopy(settingsPageCopy.sourceAddress)}</dt><dd>{sources.confirmed.source_address}</dd></div>
        <div><dt>{wrapDisplayCopy(settingsPageCopy.sourceAuthMethod)}</dt><dd>{sources.confirmed.auth_method}</dd></div>
        <div><dt>{wrapDisplayCopy(settingsPageCopy.sourceRevision)}</dt><dd>{sources.confirmed.revision_number}</dd></div>
      </dl>
    {/if}
  </section>

  <section aria-labelledby="models-title">
    <h2 id="models-title">{wrapDisplayCopy(settingsPageCopy.modelsTitle)}</h2>
    <ReadState read={models} label={settingsPageCopy.modelsLabel} onRetry={() => { void load(); }} />
    {#if models.confirmed !== null}
      {#if models.confirmed.length === 0}
        <p class="muted">{wrapDisplayCopy(settingsPageCopy.modelsEmpty)}</p>
      {:else}
        <div class="table-wrap">
          <table>
            <thead><tr><th>{wrapDisplayCopy(settingsPageCopy.model)}</th><th>{wrapDisplayCopy(settingsPageCopy.provider)}</th><th>{wrapDisplayCopy(settingsPageCopy.executorRevision)}</th></tr></thead>
            <tbody>{#each models.confirmed as configuration (configuration.agent_configuration_revision_hash)}
              <tr><td><code>{configuration.model}</code></td><td>{configuration.provider_id}</td><td><code>{configuration.executor_revision}</code></td></tr>
            {/each}</tbody>
          </table>
        </div>
      {/if}
      <p class="muted">{wrapDisplayCopy(settingsPageCopy.discovery)}</p>
    {/if}
  </section>
</section>

<style>
  .settings-page {
    display: grid;
    align-content: start;
    gap: var(--space-section);
    min-width: 0;
  }
  section { display: grid; gap: var(--space-3); }
  h1, h2 { margin: 0; }
  h2 {
    color: var(--ink-dim);
    font-size: var(--text-2xs);
    font-weight: var(--weight-heavy);
    letter-spacing: var(--tracking-label);
    text-transform: uppercase;
  }
  .muted { color: var(--ink-dim); }
  .source-list {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: var(--space-3);
    margin: 0;
  }
  .source-list div {
    border: var(--edge) solid var(--line);
    border-radius: var(--r-lg);
    padding: var(--space-3);
  }
  dt {
    color: var(--ink-dim);
    font-size: var(--text-2xs);
    letter-spacing: var(--tracking-label);
    text-transform: uppercase;
  }
  dd { margin: var(--space-1) 0 0; overflow-wrap: anywhere; }
  .table-wrap { overflow-x: auto; }
  table { border-collapse: collapse; min-width: var(--table-min); width: 100%; }
  th, td {
    border-bottom: var(--edge) solid var(--line);
    padding: var(--space-3);
    text-align: left;
    white-space: nowrap;
  }
  th {
    color: var(--ink-dim);
    font-size: var(--text-2xs);
    letter-spacing: var(--tracking-label);
    text-transform: uppercase;
  }
  @media (max-width: 32rem) {
    .source-list { grid-template-columns: 1fr 1fr; }
    .table-wrap { mask-image: linear-gradient(to right, var(--ink) calc(100% - var(--space-6)), transparent); }
  }
</style>
