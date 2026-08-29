<script lang="ts">
  import { onMount } from "svelte";

  import { CockpitRequestError, type CatalogIntakeKind, type CockpitApi, type LibraryRecognition, type Problem } from "../api/client";
  import CatalogImportSheet from "../components/CatalogImportSheet.svelte";
  import CatalogTile from "../components/CatalogTile.svelte";
  import ReadState from "../components/ReadState.svelte";
  import { catalogActivatedAt, COCKPIT_CATALOG_ACTOR, handCatalogDocumentIn } from "../lib/catalogAdmission";
  import { catalogHeadsOf, catalogNameStateOf, type CatalogNameState } from "../lib/catalogName";
  import { catalogPageCopy } from "../lib/catalogPageCopy";
  import {
    catalogAgentRows,
    catalogRowFacts,
    catalogWorkflowTiles,
    type CatalogAgentRow,
    type CatalogWorkflowRow,
    type CatalogWorkflowTiles
  } from "../lib/catalogRows";
  import { onConnectionRecovered } from "../lib/connectionState";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { humanErrorMessage } from "../lib/humanRefusal";
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
  type CatalogGroup = "all" | "workflows" | "agents" | "skills";

  let workflows: RetainedRead<CatalogWorkflowTiles, ReadFailure> =
    retainedRead<CatalogWorkflowTiles, ReadFailure>();
  let agents: RetainedRead<CatalogAgentRow[], ReadFailure> =
    retainedRead<CatalogAgentRow[], ReadFailure>();
  type ImportResult = {
    document: Uint8Array;
    recognition: LibraryRecognition | null;
    problem: Problem | null;
    failure: string | null;
  };
  type ImportFile = {
    readonly name: string;
    arrayBuffer(): Promise<ArrayBuffer>;
  };

  let fileInput: HTMLInputElement;
  let importResult: ImportResult | null = null;
  let isDropTarget = false;
  let activeGroup: CatalogGroup = "all";
  let search = "";

  $: workflowTiles = workflows.confirmed;
  $: workflowRows = workflowTiles?.rows ?? [];
  $: agentRows = agents.confirmed ?? [];
  $: catalogGroupsReady =
    workflows.confirmed !== null &&
    workflows.request.state !== "loading" &&
    agents.confirmed !== null &&
    agents.request.state !== "loading";
  $: hasCatalogEntries = workflowRows.length + agentRows.length > 0;
  $: matchingWorkflows = catalogMatches(workflowRows, search);
  $: matchingAgents = catalogMatches(agentRows, search);

  onMount(() => {
    // Navigating into the Catalog focuses the stage. On a phone that focus can
    // leave the rail above the viewport, even though it is the only room door.
    if (
      typeof globalThis.matchMedia === "function" &&
      globalThis.matchMedia("(max-width: 48rem)").matches
    ) {
      globalThis.requestAnimationFrame(() => globalThis.scrollTo({ top: 0, left: 0 }));
    }
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
        catalogWorkflowTiles(reading.revisions, catalogByName)
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

  function openFilePicker(): void {
    fileInput.click();
  }

  async function recognizeFile(file: ImportFile): Promise<void> {
    isDropTarget = false;
    try {
      const document = new Uint8Array(await file.arrayBuffer());
      importResult = {
        document,
        recognition: await cockpitApi.recognizeLibraryDocument(document, file.name),
        problem: null,
        failure: null
      };
    } catch (error) {
      importResult = {
        document: new Uint8Array(),
        recognition: null,
        problem: error instanceof CockpitRequestError ? error.problem : null,
        failure: error instanceof CockpitRequestError
          ? null
          : humanErrorMessage(error, catalogPageCopy.recognitionFailed)
      };
    }
  }

  function chooseFile(event: Event): void {
    const input = event.currentTarget as HTMLInputElement;
    const file = input.files?.[0];
    input.value = "";
    if (file !== undefined) void recognizeFile(file);
  }

  function receiveDrop(event: {
    preventDefault(): void;
    dataTransfer: { files: ArrayLike<ImportFile> } | null;
  }): void {
    event.preventDefault();
    const file = event.dataTransfer?.files[0];
    if (file !== undefined) void recognizeFile(file);
  }

  async function addLibraryDocument(document: Uint8Array, kind: CatalogIntakeKind): Promise<void> {
    await handCatalogDocumentIn(
      cockpitApi,
      document,
      kind,
      COCKPIT_CATALOG_ACTOR,
      catalogActivatedAt()
    );
    await Promise.all([loadWorkflows(), loadAgents()]);
  }

  function workflowTileStatus(
    row: CatalogWorkflowRow
  ): { label: string; description: string; dashed: boolean } | null {
    if (row.state?.kind === "not-executable") {
      return {
        label: catalogPageCopy.notExecutable,
        description: row.state.reason,
        dashed: true
      };
    }
    if (row.newerRevisionAvailable) {
      return {
        label: catalogPageCopy.newerRevision,
        description: catalogPageCopy.newerRevisionHint,
        dashed: false
      };
    }
    if (row.state?.kind === "not-admitted") {
      return {
        label: catalogPageCopy.notAdmitted,
        description: catalogPageCopy.notAdmittedHint,
        dashed: false
      };
    }
    return null;
  }

  function catalogMatches<T extends CatalogWorkflowRow | CatalogAgentRow>(
    rows: readonly T[],
    term: string
  ): T[] {
    const normalizedTerm = term.trim().toLocaleLowerCase();
    if (normalizedTerm === "") return [...rows];
    return rows.filter((row) =>
      [row.title, row.description, "provider" in row ? row.provider : ""]
        .filter((value): value is string => value !== null)
        .some((value) => value.toLocaleLowerCase().includes(normalizedTerm))
    );
  }

  function catalogGroupChoices(workflowCount: number, agentCount: number): readonly {
    group: CatalogGroup;
    label: string;
    count: number | null;
  }[] {
    return [
      { group: "all", label: catalogPageCopy.all, count: null },
      { group: "workflows", label: catalogPageCopy.workflowsTitle, count: workflowCount },
      { group: "agents", label: catalogPageCopy.agentsTitle, count: agentCount },
      { group: "skills", label: catalogPageCopy.skillsTitle, count: 0 }
    ];
  }
</script>

<section
  class="surface catalog-drop-target"
  aria-labelledby="catalog-title"
  ondragover={(event) => { event.preventDefault(); isDropTarget = true; }}
  ondragleave={(event) => { if (event.currentTarget === event.target) isDropTarget = false; }}
  ondrop={receiveDrop}
>
  <input
    bind:this={fileInput}
    class="visually-hidden"
    type="file"
    aria-label={wrapDisplayCopy(catalogPageCopy.filePicker)}
    onchange={chooseFile}
  />
  <header class="surface-head catalog-head">
    <h1 id="catalog-title">{wrapDisplayCopy(catalogPageCopy.title)}</h1>
    <button type="button" onclick={openFilePicker}>
      {wrapDisplayCopy(catalogPageCopy.import)}
    </button>
  </header>

  <ReadState read={workflows} label={catalogPageCopy.workflowsLabel} onRetry={() => { void loadWorkflows(); }} />
  <ReadState read={agents} label={catalogPageCopy.agentsLabel} onRetry={() => { void loadAgents(); }} />

  {#if hasCatalogEntries}
    {#if catalogGroupsReady}
      <div class="catalog-filters" role="group" aria-label={wrapDisplayCopy(catalogPageCopy.catalogGroups)}>
        {#each catalogGroupChoices(workflowTiles?.count ?? 0, agentRows.length) as { group, label, count } (group)}
          <button
            class="filter-chip"
            class:active={activeGroup === group}
            type="button"
            aria-pressed={activeGroup === group}
            onclick={() => { activeGroup = group as CatalogGroup; }}
          >{wrapDisplayCopy(label)}{#if count !== null} <b>{count}</b>{/if}</button>
        {/each}
        <input bind:value={search} type="search" placeholder={wrapDisplayCopy(catalogPageCopy.search)} aria-label={wrapDisplayCopy(catalogPageCopy.searchLabel)} />
      </div>
    {/if}

    {#if activeGroup === "all" || activeGroup === "workflows"}
      <section aria-label={wrapDisplayCopy(catalogPageCopy.workflowsTitle)}>
        <ul class="tile-grid">
          {#each matchingWorkflows as row (row.revisionHash)}
            <CatalogTile
              kind="workflow"
              title={row.title}
              ariaLabel={wrapDisplayCopy(row.title)}
              description={row.description ?? wrapDisplayCopy(catalogPageCopy.noDescription)}
              provenance={catalogRowFacts()}
              href={row.name === null ? null : workflowPath(row.name)}
              status={workflowTileStatus(row)}
              onOpen={navigate}
            />
          {/each}
        </ul>
      </section>
    {/if}

    {#if activeGroup === "all" || activeGroup === "agents"}
      <section aria-label={wrapDisplayCopy(catalogPageCopy.agentsByProvider)}>
        <ul class="tile-grid">
          {#each matchingAgents as row (row.revisionHash)}
            <CatalogTile
              kind="agent"
              title={row.title}
              ariaLabel={wrapDisplayCopy(row.title)}
              description={row.description}
              provenance={catalogRowFacts()}
              provider={wrapDisplayCopy(row.provider)}
            />
          {/each}
        </ul>
      </section>
    {/if}

    {#if activeGroup === "skills"}
      <section aria-label={wrapDisplayCopy(catalogPageCopy.skillsTitle)}>
        <p class="empty"><span aria-hidden="true">✦ </span><span>{wrapDisplayCopy(catalogPageCopy.skillsNone)}</span></p>
      </section>
    {/if}
  {:else if
    workflows.confirmed !== null &&
    workflows.request.state === "idle" &&
    agents.confirmed !== null &&
    agents.request.state === "idle"}
    <p class="empty">{wrapDisplayCopy(catalogPageCopy.catalogEmpty)}</p>
  {/if}

  {#if isDropTarget}
    <div class="drop-veil" aria-hidden="true">
      <p>{wrapDisplayCopy(catalogPageCopy.catalogEmpty)}</p>
    </div>
  {/if}

  {#if importResult !== null}
    <CatalogImportSheet
      document={importResult.document}
      recognition={importResult.recognition}
      recognitionProblem={importResult.problem}
      recognitionFailure={importResult.failure}
      add={addLibraryDocument}
      onClose={() => { importResult = null; }}
    />
  {/if}
</section>

<style>
  p {
    margin: 0;
  }

  .catalog-head {
    display: flex;
    align-items: start;
    justify-content: space-between;
    gap: var(--space-4);
  }

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

  .catalog-drop-target {
    position: relative;
  }

  .drop-veil {
    position: absolute;
    z-index: 1;
    inset: 0;
    display: grid;
    place-items: center;
    padding: var(--space-4);
    background: color-mix(in srgb, var(--ground) 80%, transparent);
  }

  .drop-veil p {
    max-width: var(--reading-width);
    padding: var(--space-4) var(--space-5);
    border: var(--edge-strong) dashed var(--ink);
    border-radius: var(--r-lg);
    background: var(--panel2);
    font-family: var(--serif);
    text-align: center;
  }

  .empty {
    padding: var(--space-3) var(--space-4);
    border: var(--edge) solid var(--line);
    border-radius: var(--r-lg);
    background: var(--panel2);
    color: var(--ink-dim);
    font-size: var(--text-xs);
  }

  .catalog-filters {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-1) var(--space-3);
    padding-bottom: var(--space-2);
    border-bottom: var(--edge) solid var(--line);
  }

  .filter-chip {
    min-height: var(--tap);
    padding: var(--space-2) 0;
    border: 0;
    border-bottom: var(--edge-strong) solid transparent;
    border-radius: 0;
    color: var(--ink-dim);
    background: transparent;
    font-size: var(--text-2xs);
    font-weight: var(--weight-heavy);
    letter-spacing: var(--tracking-label);
    text-transform: uppercase;
  }

  .filter-chip.active {
    border-bottom-color: var(--ink);
    color: var(--ink);
    background: transparent;
  }

  .filter-chip b {
    margin-left: var(--space-1);
    color: var(--ink);
  }

  .catalog-filters input {
    width: min(var(--catalog-search-width), 100%);
    min-height: var(--tap);
    margin-left: auto;
    border: var(--edge) solid var(--line);
    border-radius: var(--r);
    padding: var(--space-2) var(--space-3);
    color: var(--ink);
    background: var(--panel2);
    font-size: var(--text-xs);
  }

  .tile-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(var(--card-min), 1fr));
    gap: var(--space-3);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  /* At phone width the rail is one row. Its former flex layout gave Settings'
     project context an unshrinkable width, which pushed History and Settings
     onto a second line and left the Catalog action outside the viewport. The
     grid reserves room for the room doors and lets only the context elide. */
  @media (max-width: 48rem) {
    :global(.workshop .workshop-rail) {
      display: grid;
      grid-template-columns: max-content repeat(3, max-content) minmax(0, 1fr);
      gap: var(--space-1);
      padding: var(--space-2);
    }

    :global(.workshop .workshop-rail .rail-brand) {
      padding: var(--space-1);
      font-size: var(--text-sm);
    }

    :global(.workshop .workshop-rail .nav-destination) {
      gap: var(--space-1);
      min-width: 0;
      padding: var(--space-1);
      font-size: var(--text-2xs);
    }

    :global(.workshop .workshop-rail .nav-destination-mark) {
      width: auto;
    }

    :global(.workshop .workshop-rail .rail-grow) {
      display: none;
    }

    :global(.workshop .workshop-rail > a:nth-of-type(1)) { grid-column: 2; grid-row: 1; }
    :global(.workshop .workshop-rail > a:nth-of-type(2)) { grid-column: 3; grid-row: 1; }
    :global(.workshop .workshop-rail > a:nth-of-type(3)) { grid-column: 4; grid-row: 1; }

    :global(.workshop .workshop-rail .rail-foot) {
      grid-column: 5;
      grid-row: 1;
      margin-left: 0;
      justify-self: end;
    }

    :global(.workshop .workshop-rail .rail-project) {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
  }

  @media (max-width: 48rem) {
    .catalog-filters input {
      margin-left: auto;
    }
  }

</style>
