<script lang="ts">
  import { onMount } from "svelte";

  import { CockpitRequestError, type LibraryRecognition, type Problem } from "../api/client";
  import { catalogPageCopy } from "../lib/catalogPageCopy";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { humanErrorMessage } from "../lib/humanRefusal";
  import ProblemNotice from "./ProblemNotice.svelte";

  export let document: Uint8Array;
  export let fileName: string;
  export let recognition: LibraryRecognition | null;
  export let recognitionProblem: Problem | null;
  export let recognitionFailure: string | null;
  export let add: (document: Uint8Array, fileName: string | null) => Promise<void>;
  export let onClose: () => void;

  type DialogElement = HTMLElement & { open: boolean; showModal?: () => void; close?: () => void };

  let dialogElement: DialogElement;
  let closeButton: HTMLButtonElement;
  let primaryAction: HTMLButtonElement;
  let adding = false;
  let problem: Problem | null = null;
  let failure: string | null = null;

  onMount(() => {
    if (typeof dialogElement.showModal === "function") dialogElement.showModal();
    else dialogElement.open = true;
    if (recognition !== null && canAdd(recognition)) primaryAction.focus();
    else closeButton.focus();
  });

  function dismiss(): void {
    dialogElement.close?.();
    onClose();
  }

  async function addRecognizedDocument(): Promise<void> {
    if (recognition === null || !canAdd(recognition)) return;
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

  function mustClose(): boolean {
    return recognition === null || !canAdd(recognition);
  }

  function recognitionLabel(value: LibraryRecognition): string {
    if (value.outcome === "workflow") return value.name ?? catalogPageCopy.unnamedWorkflow;
    if (value.outcome === "agent_definition") return value.name;
    return "";
  }

  function recognitionGlyph(value: LibraryRecognition): string {
    return value.outcome === "workflow" ? "⧉" : "◯";
  }

  function recognitionCount(value: LibraryRecognition): string {
    return value.outcome === "workflow" ? catalogPageCopy.oneWorkflow : catalogPageCopy.oneAgent;
  }
</script>

<div class="sheet-positioner">
  <dialog bind:this={dialogElement} class="sheet" aria-labelledby="catalog-import-title" oncancel={dismiss}>
    <header>
      <h2 id="catalog-import-title">{wrapDisplayCopy(catalogPageCopy.import)}</h2>
    </header>

    {#if recognition !== null && canAdd(recognition)}
      <div class="found">
        <span class="glyph" aria-hidden="true">{recognitionGlyph(recognition)}</span>
        <span class="count">{wrapDisplayCopy(recognitionCount(recognition))}</span>
        <strong>{recognitionLabel(recognition)}</strong>
      </div>
    {:else if recognition?.outcome === "not_held"}
      <p class="failure" role="alert">{recognition.reason}</p>
    {:else if recognition?.outcome === "unrecognized"}
      <p class="failure" role="alert">{wrapDisplayCopy(catalogPageCopy.unrecognized)}</p>
    {/if}

    {#if recognitionProblem !== null}
      <ProblemNotice title={catalogPageCopy.importFailed} problem={recognitionProblem} />
    {/if}
    {#if recognitionFailure !== null}
      <p class="failure" role="alert">{recognitionFailure}</p>
    {/if}
    {#if problem !== null}
      <ProblemNotice title={catalogPageCopy.importFailed} {problem} />
    {/if}
    {#if failure !== null}
      <p class="failure" role="alert">{failure}</p>
    {/if}

    <footer>
      {#if recognition !== null && canAdd(recognition)}
        <button bind:this={primaryAction} class="primary" type="button" disabled={adding} onclick={() => { void addRecognizedDocument(); }}>
          {wrapDisplayCopy(adding ? catalogPageCopy.addingToCatalog : catalogPageCopy.addToCatalog)}
        </button>
      {/if}
      <button bind:this={closeButton} class="quiet" type="button" disabled={adding} onclick={dismiss}>
        {wrapDisplayCopy(mustClose() ? catalogPageCopy.close : catalogPageCopy.cancel)}
      </button>
    </footer>
  </dialog>
</div>

<style>
  .sheet-positioner { position: fixed; inset: 0; z-index: 10; pointer-events: none; }
  .sheet { box-sizing: border-box; position: fixed; inset: var(--space-5) var(--space-5) auto auto; width: min(var(--dialog-width), calc(100% - (var(--space-5) * 2))); max-height: calc(100vh - (var(--space-5) * 2)); margin: 0; padding: var(--space-5); background: var(--panel2); border: var(--edge) solid var(--ink); border-radius: var(--r-lg); box-shadow: var(--shadow-lift); pointer-events: auto; }
  .sheet::backdrop { background: color-mix(in srgb, var(--ground) 80%, transparent); }
  header, footer { display: flex; align-items: center; gap: var(--space-3); }
  h2 { margin: 0; font-family: var(--serif); }
  .found { display: grid; grid-template-columns: auto auto minmax(0, 1fr); align-items: baseline; gap: var(--space-2); margin-bottom: var(--space-4); }
  .glyph { color: var(--ink-dim); }
  .count { color: var(--ink-dim); font-size: var(--text-2xs); }
  .failure { color: var(--signal-failure); }
  @media (max-width: 480px) { .count { display: none; } }
  @media (max-width: 480px) { .sheet { inset: auto 0 0 0; width: 100%; max-height: 85vh; border-radius: var(--r-lg) var(--r-lg) 0 0; } }
</style>
