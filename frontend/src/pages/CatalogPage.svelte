<script lang="ts">
  import { onMount } from "svelte";

  import type { CockpitApi } from "../api/client";
  import CatalogImportDoor from "../components/CatalogImportDoor.svelte";
  import ReadState from "../components/ReadState.svelte";
  import {
    admitPublishedRevision,
    catalogActivatedAt,
    COCKPIT_CATALOG_ACTOR
  } from "../lib/catalogAdmission";
  import { catalogHeadsOf, catalogNameStateOf, type CatalogNameState } from "../lib/catalogName";
  import { catalogPageCopy } from "../lib/catalogPageCopy";
  import {
    catalogAgentRows,
    catalogRowFacts,
    catalogWorkflowRows,
    type CatalogAgentRow,
    type CatalogWorkflowRow
  } from "../lib/catalogRows";
  import { onConnectionRecovered } from "../lib/connectionState";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { humanErrorMessage } from "../lib/humanRefusal";
  import { publicationMutation } from "../lib/mutationJournal";
  import {
    beginRead,
    confirmRead,
    failRead,
    retainedRead,
    type RetainedRead
  } from "../lib/readResource";
  import { workflowPath } from "../lib/route";
  import { readEveryRevision } from "../lib/runPages";

  export let cockpitApi: CockpitApi;
  export let navigate: (path: string) => void;

  type ReadFailure =
    | { kind: "unavailable"; title: string }
    | { kind: "incomplete"; title: string };

  let workflows: RetainedRead<CatalogWorkflowRow[], ReadFailure> =
    retainedRead<CatalogWorkflowRow[], ReadFailure>();
  let agents: RetainedRead<CatalogAgentRow[], ReadFailure> =
    retainedRead<CatalogAgentRow[], ReadFailure>();
  let admittingHash: string | null = null;
  let admissionFailure: string | null = null;

  onMount(() => {
    void loadWorkflows();
    void loadAgents();
    // A read that failed while the connection was lost is worth asking again
    // on its own once it returns, with no reload (#700).
    return onConnectionRecovered(() => {
      void loadWorkflows();
      void loadAgents();
    });
  });

  async function loadWorkflows(): Promise<void> {
    const begun = beginRead(workflows);
    workflows = begun.read;
    try {
      const reading = await readEveryRevision((after) => cockpitApi.listWorkflowRevisions(after));
      if (!reading.complete) {
        workflows = failRead(workflows, begun.generation, {
          kind: "incomplete",
          title: catalogPageCopy.workflowsIncomplete
        });
        return;
      }
      const catalogByName = await catalogStates(reading.revisions);
      // A name whose admitted head is not among the revisions just read means
      // the two reads are not one snapshot; saying so beats drawing a row that
      // is startable in one answer and not in the other.
      if (catalogHeadsOf(reading.revisions, catalogByName) === null) {
        workflows = failRead(workflows, begun.generation, {
          kind: "unavailable",
          title: catalogPageCopy.workflowsUnavailable
        });
        return;
      }
      workflows = confirmRead(
        workflows,
        begun.generation,
        catalogWorkflowRows(reading.revisions, catalogByName)
      );
    } catch {
      workflows = failRead(workflows, begun.generation, {
        kind: "unavailable",
        title: catalogPageCopy.workflowsUnavailable
      });
    }
  }

  async function catalogStates(
    revisions: readonly { name: string | null }[]
  ): Promise<Record<string, CatalogNameState>> {
    const names = [
      ...new Set(revisions.flatMap((item) => (item.name === null ? [] : [item.name])))
    ];
    const states = await Promise.all(
      names.map(async (name) => [
        name,
        await catalogNameStateOf(name, (asked) => cockpitApi.getRevisionByName(asked))
      ])
    );
    return Object.fromEntries(states) as Record<string, CatalogNameState>;
  }

  async function loadAgents(): Promise<void> {
    const begun = beginRead(agents);
    agents = begun.read;
    try {
      const page = await cockpitApi.listAgentDefinitionRevisions();
      agents = confirmRead(agents, begun.generation, catalogAgentRows(page.items));
    } catch {
      agents = failRead(agents, begun.generation, {
        kind: "unavailable",
        title: catalogPageCopy.agentsUnavailable
      });
    }
  }

  /**
   * Publication is idempotent by hash, so this door needs no journal.
   *
   * The start door journals its publication because a lost response there
   * leaves an exact request nobody can replay. Here the operator still holds
   * the file, and importing it again answers with the same revision — so the
   * retry is the Import button, not a pending-request list.
   */
  async function importWorkflow(exactYaml: string): Promise<void> {
    await cockpitApi.publish(await publicationMutation(exactYaml));
    await loadWorkflows();
  }

  async function importAgent(exactMarkdown: string): Promise<void> {
    await cockpitApi.publishAgentDefinition(exactMarkdown);
    await loadAgents();
  }

  async function admit(revisionHash: string): Promise<void> {
    admittingHash = revisionHash;
    admissionFailure = null;
    try {
      const revision = await cockpitApi.getWorkflowRevision(revisionHash);
      await admitPublishedRevision(
        cockpitApi,
        revision,
        COCKPIT_CATALOG_ACTOR,
        catalogActivatedAt()
      );
      await loadWorkflows();
    } catch (error) {
      admissionFailure = humanErrorMessage(error, catalogPageCopy.admitFailed);
    } finally {
      admittingHash = null;
    }
  }
</script>

<section class="surface" aria-labelledby="catalog-title">
  <header class="surface-head">
    <h1 id="catalog-title">{wrapDisplayCopy(catalogPageCopy.title)}</h1>
    <p>{wrapDisplayCopy(catalogPageCopy.lead)}</p>
  </header>

  <section aria-labelledby="catalog-workflows-title">
    <h2 id="catalog-workflows-title">{wrapDisplayCopy(catalogPageCopy.workflowsTitle)}</h2>
    <ReadState read={workflows} label="workflows" onRetry={() => { void loadWorkflows(); }} />
    {#if admissionFailure !== null}<p class="failure" role="alert">{admissionFailure}</p>{/if}

    {#if workflows.confirmed !== null && workflows.confirmed.length === 0}
      <p class="empty">{wrapDisplayCopy(catalogPageCopy.workflowsEmpty)}</p>
    {/if}

    <ul class="entries">
      {#each workflows.confirmed ?? [] as row (row.revisionHash)}
        <li class="entry card">
          <div class="entry-head">
            <strong>{row.title}</strong>
            {#if row.state?.kind === "startable"}
              <span class="entry-state">{wrapDisplayCopy(catalogPageCopy.startable)}</span>
            {:else if row.state?.kind === "not-admitted"}
              <span class="entry-state attention">{wrapDisplayCopy(catalogPageCopy.notAdmitted)}</span>
            {:else if row.state?.kind === "not-executable"}
              <span class="entry-state failed">{wrapDisplayCopy(catalogPageCopy.notExecutable)}</span>
            {/if}
            {#if row.newerRevisionAvailable}
              <span class="entry-state attention"
                >{wrapDisplayCopy(catalogPageCopy.newerRevisionAvailable)}</span
              >
            {/if}
          </div>
          <p class="entry-line">{row.description ?? wrapDisplayCopy(catalogPageCopy.noDescription)}</p>
          {#if row.state?.kind === "not-executable"}
            <p class="entry-line failure">{row.state.reason}</p>
          {/if}
          <p class="entry-facts">{catalogRowFacts(row.revisionHash).join(" · ")}</p>
          {#if row.name !== null}
            {@const detailPath = workflowPath(row.name)}
            <div class="entry-actions">
              {#if row.state?.kind === "not-admitted" && row.admittable}
                <button
                  type="button"
                  disabled={admittingHash !== null}
                  onclick={() => { void admit(row.revisionHash); }}
                  >{wrapDisplayCopy(
                    admittingHash === row.revisionHash
                      ? catalogPageCopy.admitting
                      : catalogPageCopy.admit
                  )}</button
                >
              {:else if row.state?.kind === "startable"}
                <a
                  class="button"
                  href="/atelier/workflows"
                  onclick={(event) => { event.preventDefault(); navigate("/atelier/workflows"); }}
                  >{wrapDisplayCopy(catalogPageCopy.start)}</a
                >
              {/if}
              <a
                class="button"
                href={detailPath}
                onclick={(event) => { event.preventDefault(); navigate(detailPath); }}
                >{wrapDisplayCopy(catalogPageCopy.details)}</a
              >
            </div>
          {/if}
        </li>
      {/each}
    </ul>

    <CatalogImportDoor
      title={catalogPageCopy.importWorkflowTitle}
      hint={catalogPageCopy.importWorkflowHint}
      label={catalogPageCopy.importWorkflowLabel}
      accept=".yaml,.yml,text/yaml,application/yaml"
      fieldId="import-workflow"
      failureTitle={catalogPageCopy.importWorkflowFailed}
      onImport={importWorkflow}
    />
  </section>

  <section aria-labelledby="catalog-agents-title">
    <h2 id="catalog-agents-title">{wrapDisplayCopy(catalogPageCopy.agentsTitle)}</h2>
    <ReadState read={agents} label="agents" onRetry={() => { void loadAgents(); }} />

    {#if agents.confirmed !== null && agents.confirmed.length === 0}
      <p class="empty">{wrapDisplayCopy(catalogPageCopy.agentsEmpty)}</p>
    {/if}

    <ul class="entries">
      {#each agents.confirmed ?? [] as row (row.revisionHash)}
        <li class="entry card">
          <div class="entry-head">
            <strong>{row.title}</strong>
            <span class="entry-provider">{wrapDisplayCopy(row.provider)}</span>
            <span class="entry-state">{wrapDisplayCopy(catalogPageCopy.agentPublishedOnly)}</span>
          </div>
          <p class="entry-line">{row.description}</p>
          <p class="entry-facts">{catalogRowFacts(row.revisionHash).join(" · ")}</p>
        </li>
      {/each}
    </ul>

    <CatalogImportDoor
      title={catalogPageCopy.importAgentTitle}
      hint={catalogPageCopy.importAgentHint}
      label={catalogPageCopy.importAgentLabel}
      accept=".md,text/markdown"
      fieldId="import-agent"
      failureTitle={catalogPageCopy.importAgentFailed}
      onImport={importAgent}
    />
  </section>

  <section aria-labelledby="catalog-skills-title">
    <h2 id="catalog-skills-title">{wrapDisplayCopy(catalogPageCopy.skillsTitle)}</h2>
    <p class="empty">{wrapDisplayCopy(catalogPageCopy.skillsNone)}</p>
  </section>
</section>

<style>
  h2 {
    margin: 0 0 var(--space-3);
    font-size: var(--text-lg);
  }

  p {
    margin: 0;
  }

  .empty {
    color: var(--ink-dim);
    font-size: var(--text-sm);
  }

  /* The provider is what an imported agent belongs to, so it wears the ink of
     a fact rather than the colour of a state. */
  .entry-provider {
    border: var(--edge) solid var(--line);
    border-radius: var(--r-pill);
    padding: 0 var(--space-2);
    font-size: var(--text-2xs);
  }

  .failure {
    color: var(--signal-failure);
  }

  .entries {
    display: grid;
    gap: var(--space-3);
    margin: 0 0 var(--space-4);
    padding: 0;
    list-style: none;
  }

  .entry {
    display: grid;
    gap: var(--space-1);
    justify-items: start;
  }

  .entry-head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--space-2);
  }

  .entry strong {
    font-size: var(--text-sm);
  }

  .entry-line {
    font-size: var(--text-xs);
  }

  .entry-facts {
    color: var(--ink-dim);
    font-size: var(--text-2xs);
  }

  .entry-actions {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    margin-top: var(--space-2);
  }

  /* Colour is for what asks of you, so a state that asks nothing — startable,
     or an agent that is simply published — stays ink. What waits for an
     admission calls in clay; what cannot run calls in brick. */
  .entry-state {
    color: var(--ink-dim);
    font-size: var(--text-2xs);
    text-transform: uppercase;
    letter-spacing: var(--tracking-label);
  }

  .entry-state.attention {
    color: var(--signal-attention);
  }

  .entry-state.failed {
    color: var(--signal-failure);
  }
</style>
