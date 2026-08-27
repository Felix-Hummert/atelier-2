<script lang="ts">
  import { onMount } from "svelte";

  import { CockpitRequestError, type CockpitApi, type LibraryRecognition, type Problem } from "../api/client";
  import CatalogImportSheet from "../components/CatalogImportSheet.svelte";
  import InfoHint from "../components/InfoHint.svelte";
  import ReadState from "../components/ReadState.svelte";
  import { catalogActivatedAt, COCKPIT_CATALOG_ACTOR } from "../lib/catalogAdmission";
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
  type ImportResult = {
    document: Uint8Array;
    fileName: string;
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

  function openFilePicker(): void {
    fileInput.click();
  }

  async function recognizeFile(file: ImportFile): Promise<void> {
    isDropTarget = false;
    try {
      const document = new Uint8Array(await file.arrayBuffer());
      importResult = {
        document,
        fileName: file.name,
        recognition: await cockpitApi.recognizeLibraryDocument(document, file.name),
        problem: null,
        failure: null
      };
    } catch (error) {
      importResult = {
        document: new Uint8Array(),
        fileName: file.name,
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

  async function addLibraryDocument(document: Uint8Array, fileName: string | null): Promise<void> {
    await cockpitApi.addLibraryDocument(
      document,
      fileName,
      COCKPIT_CATALOG_ACTOR,
      catalogActivatedAt()
    );
    await Promise.all([loadWorkflows(), loadAgents()]);
  }

  function workflowStateHint(row: CatalogWorkflowRow): string | null {
    if (row.state?.kind === "not-executable") {
      return row.state.reason;
    }
    if (row.newerRevisionAvailable) return catalogPageCopy.newerRevisionHint;
    if (row.state?.kind === "not-admitted") return catalogPageCopy.notAdmittedHint;
    return null;
  }
</script>

<section
  class="surface catalog-drop-target"
  class:drop-target-active={isDropTarget}
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
    <div>
      <h1 id="catalog-title">{wrapDisplayCopy(catalogPageCopy.title)}</h1>
      <p>{wrapDisplayCopy(catalogPageCopy.lead)}</p>
    </div>
    <button type="button" onclick={openFilePicker}>
      {wrapDisplayCopy(catalogPageCopy.import)}
    </button>
  </header>

  <section aria-labelledby="catalog-workflows-title">
    <h2 id="catalog-workflows-title">{wrapDisplayCopy(catalogPageCopy.workflowsTitle)}</h2>
    <ReadState read={workflows} label="workflows" onRetry={() => { void loadWorkflows(); }} />
    {#if workflows.confirmed !== null && workflows.confirmed.length === 0}
      <p class="empty">{wrapDisplayCopy(catalogPageCopy.workflowsEmpty)}</p>
    {/if}

    <ul class="entries">
      {#each workflows.confirmed ?? [] as row (row.revisionHash)}
        {@const stateHint = workflowStateHint(row)}
        <li
          class="entry card"
          class:marked-attention={stateHint !== null && row.state?.kind !== "not-executable"}
          class:marked-blocked={row.state?.kind === "not-executable"}
        >
          {#if row.name !== null}
            {@const detailPath = workflowPath(row.name)}
            <a
              class="entry-door"
              href={detailPath}
              aria-label={row.title}
              onclick={(event) => { event.preventDefault(); navigate(detailPath); }}
            >
              <div class="entry-head">
                <strong>{row.title}</strong>
              </div>
              <p class="entry-line">{row.description ?? wrapDisplayCopy(catalogPageCopy.noDescription)}</p>
              <p class="entry-facts">{catalogRowFacts().join(" · ")}</p>
            </a>
          {:else}
            <div class="entry-head">
              <strong>{row.title}</strong>
            </div>
            <p class="entry-line">{row.description ?? wrapDisplayCopy(catalogPageCopy.noDescription)}</p>
            <p class="entry-facts">{catalogRowFacts().join(" · ")}</p>
          {/if}
          {#if stateHint !== null}
            <InfoHint
              label={wrapDisplayCopy(catalogPageCopy.stateHint)}
              prose={wrapDisplayCopy(stateHint)}
              text={wrapDisplayCopy(catalogPageCopy.why)}
              pinToCard={true}
            />
          {/if}
        </li>
      {/each}
    </ul>

  </section>

  <section aria-labelledby="catalog-agents-title">
    <h2 id="catalog-agents-title">{wrapDisplayCopy(catalogPageCopy.agentsTitle)}</h2>
    <ReadState read={agents} label="agents" onRetry={() => { void loadAgents(); }} />

    {#if agents.confirmed !== null && agents.confirmed.length === 0}
      <p class="empty">{wrapDisplayCopy(catalogPageCopy.agentsEmpty)}</p>
    {/if}

    <ul class="entries">
      {#each agents.confirmed ?? [] as row (row.revisionHash)}
        <li class="entry card marked-blocked">
          <div class="entry-head">
            <strong>{row.title}</strong>
            <span class="entry-provider">{wrapDisplayCopy(row.provider)}</span>
          </div>
          <InfoHint
            label={wrapDisplayCopy(catalogPageCopy.stateHint)}
            prose={wrapDisplayCopy(catalogPageCopy.agentUnavailableHint)}
            text={wrapDisplayCopy(catalogPageCopy.why)}
            pinToCard={true}
          />
          <p class="entry-line">{row.description}</p>
          <p class="entry-facts">{catalogRowFacts().join(" · ")}</p>
        </li>
      {/each}
    </ul>

  </section>

  <section aria-labelledby="catalog-skills-title">
    <h2 id="catalog-skills-title">{wrapDisplayCopy(catalogPageCopy.skillsTitle)}</h2>
    <p class="empty">{wrapDisplayCopy(catalogPageCopy.skillsNone)}</p>
  </section>

  {#if importResult !== null}
    <CatalogImportSheet
      document={importResult.document}
      fileName={importResult.fileName}
      recognition={importResult.recognition}
      recognitionProblem={importResult.problem}
      recognitionFailure={importResult.failure}
      add={addLibraryDocument}
      onClose={() => { importResult = null; }}
    />
  {/if}
</section>

<style>
  h2 {
    margin: 0 0 var(--space-3);
    font-size: var(--text-lg);
  }

  p {
    margin: 0;
  }

  .catalog-head {
    display: flex;
    align-items: start;
    justify-content: space-between;
    gap: var(--space-4);
  }

  .catalog-head > div {
    display: grid;
    gap: var(--space-1);
    min-width: 0;
  }

  .catalog-head p {
    max-width: var(--reading-width);
    color: var(--ink-dim);
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

  .catalog-drop-target.drop-target-active {
    outline: var(--edge-strong) dashed var(--signal-attention);
    outline-offset: calc(var(--space-2) * -1);
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
    position: relative;
    border-left: var(--edge-mark) solid transparent;
  }

  .entry.marked-attention {
    border-left-color: var(--signal-attention-mark);
    border-left-style: solid;
    background: color-mix(in srgb, var(--signal-attention-mark) var(--wash), var(--panel2));
  }

  .entry.marked-blocked {
    border-left-color: var(--signal-failure);
    border-left-style: dashed;
    background: color-mix(in srgb, var(--signal-failure) var(--wash), var(--panel2));
  }

  .entry-head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: var(--space-2);
  }

  .entry-door {
    display: grid;
    gap: var(--space-1);
    width: 100%;
    color: inherit;
    text-decoration: none;
  }

  .entry-door:hover strong,
  .entry-door:focus-visible strong {
    text-decoration: underline;
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

</style>
