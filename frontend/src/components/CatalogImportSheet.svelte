<script lang="ts">
  import { onMount } from "svelte";

  import { CockpitRequestError, type LibraryRecognition, type Problem } from "../api/client";
  import { catalogPageCopy } from "../lib/catalogPageCopy";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { humanErrorMessage } from "../lib/humanRefusal";
  import ProblemNotice from "./ProblemNotice.svelte";

  export let recognize: (
    document: Uint8Array,
    fileName: string | null
  ) => Promise<LibraryRecognition>;
  export let add: (document: Uint8Array, fileName: string | null) => Promise<void>;
  export let onClose: () => void;

  type DialogElement = HTMLElement & { open: boolean; showModal?: () => void; close?: () => void };

  let dialogElement: DialogElement;
  let closeButton: HTMLButtonElement;
  let document: Uint8Array | null = null;
  let fileName: string | null = null;
  let recognition: LibraryRecognition | null = null;
  let recognizing = false;
  let adding = false;
  let problem: Problem | null = null;
  let failure: string | null = null;

  onMount(() => {
    if (typeof dialogElement.showModal === "function") dialogElement.showModal();
    else dialogElement.open = true;
    closeButton.focus();
  });

  function dismiss(): void {
    dialogElement.close?.();
    onClose();
  }

  function clearVerdict(): void {
    recognition = null;
    problem = null;
    failure = null;
  }

  async function chooseFile(event: Event): Promise<void> {
    const chosen = (event.currentTarget as HTMLInputElement).files?.[0];
    if (chosen === undefined) return;
    clearVerdict();
    document = null;
    fileName = chosen.name;
    recognizing = true;
    try {
      document = new Uint8Array(await chosen.arrayBuffer());
      recognition = await recognize(document, fileName);
    } catch (error) {
      if (error instanceof CockpitRequestError && error.problem !== null) problem = error.problem;
      else failure = humanErrorMessage(error, catalogPageCopy.recognitionFailed);
    } finally {
      recognizing = false;
    }
  }

  async function addRecognizedDocument(): Promise<void> {
    if (document === null || recognition === null || !canAdd(recognition)) return;
    problem = null;
    failure = null;
    adding = true;
    try {
      await add(document, fileName);
      dismiss();
    } catch (error) {
      if (error instanceof CockpitRequestError && error.problem !== null) problem = error.problem;
      else failure = humanErrorMessage(error, catalogPageCopy.importFailed);
    } finally {
      adding = false;
    }
  }

  function canAdd(value: LibraryRecognition): boolean {
    return value.outcome === "workflow" || value.outcome === "agent_definition";
  }

  function recognitionLabel(value: LibraryRecognition): string {
    if (value.outcome === "workflow") return value.name ?? catalogPageCopy.unnamedWorkflow;
    if (value.outcome === "agent_definition") return value.name;
    return "";
  }

  function recognitionDescription(value: LibraryRecognition): string | null {
    if (value.outcome === "workflow" || value.outcome === "agent_definition") {
      return value.description;
    }
    return null;
  }
</script>

<div class="sheet-positioner">
  <dialog bind:this={dialogElement} class="sheet" aria-labelledby="catalog-import-title" oncancel={dismiss}>
    <header>
      <h2 id="catalog-import-title">{wrapDisplayCopy(catalogPageCopy.import)}</h2>
    </header>

    <label class="file-choice button">
      {wrapDisplayCopy(catalogPageCopy.chooseFile)}
      <input
        class="visually-hidden"
        type="file"
        disabled={recognizing || adding}
        onchange={(event) => { void chooseFile(event); }}
      />
    </label>

    {#if recognizing}
      <p>{wrapDisplayCopy(catalogPageCopy.recognizing)}</p>
    {:else if recognition !== null && canAdd(recognition)}
      <div class="found">
        <p class="kind">{wrapDisplayCopy(recognition.outcome === "workflow" ? catalogPageCopy.workflow : catalogPageCopy.agent)}</p>
        <strong>{recognitionLabel(recognition)}</strong>
        {#if recognitionDescription(recognition) !== null}
          <p>{recognitionDescription(recognition)}</p>
        {/if}
      </div>
    {:else if recognition?.outcome === "not_held"}
      <p class="failure" role="alert">{recognition.reason}</p>
    {:else if recognition?.outcome === "unrecognized"}
      <p class="failure" role="alert">{wrapDisplayCopy(catalogPageCopy.unrecognized)}</p>
    {/if}

    {#if problem !== null}
      <ProblemNotice title={catalogPageCopy.importFailed} {problem} />
    {/if}
    {#if failure !== null}
      <p class="failure" role="alert">{failure}</p>
    {/if}

    <footer>
      {#if recognition !== null && canAdd(recognition)}
        <button class="primary" type="button" disabled={adding} onclick={() => { void addRecognizedDocument(); }}>
          {wrapDisplayCopy(adding ? catalogPageCopy.addingToCatalog : catalogPageCopy.addToCatalog)}
        </button>
      {/if}
      <button bind:this={closeButton} class="quiet" type="button" disabled={adding} onclick={dismiss}>
        {wrapDisplayCopy(recognition?.outcome === "unrecognized" || recognition?.outcome === "not_held" ? catalogPageCopy.close : catalogPageCopy.cancel)}
      </button>
    </footer>
  </dialog>
</div>

<style>
  .sheet-positioner { position: fixed; inset: 0; z-index: 10; pointer-events: none; }
  .sheet { box-sizing: border-box; position: fixed; inset: var(--space-5) var(--space-5) auto auto; width: min(var(--dialog-width), calc(100% - (var(--space-5) * 2))); max-height: calc(100vh - (var(--space-5) * 2)); margin: 0; padding: var(--space-5); background: var(--panel2); border: var(--edge) solid var(--ink); border-radius: var(--r-lg); box-shadow: var(--shadow-lift); pointer-events: auto; }
  .sheet::backdrop { background: color-mix(in srgb, var(--ground) 80%, transparent); }
  header, footer { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); }
  h2 { margin: 0; font-family: var(--serif); }
  .file-choice { display: inline-flex; align-items: center; margin: var(--space-4) 0; cursor: pointer; }
  .file-choice:has(input:focus-visible) { outline: var(--edge) solid var(--accent); outline-offset: var(--space-1); }
  .visually-hidden { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
  .found { display: grid; gap: var(--space-1); margin-bottom: var(--space-4); }
  .found p { margin: 0; }
  .kind { color: var(--ink-dim); font-size: var(--text-2xs); font-weight: var(--weight-strong); letter-spacing: var(--tracking-label); text-transform: uppercase; }
  .failure { color: var(--signal-failure); }
  @media (max-width: 480px) { .sheet { inset: auto 0 0 0; width: 100%; max-height: 85vh; border-radius: var(--r-lg) var(--r-lg) 0 0; } }
</style>
