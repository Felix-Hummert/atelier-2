<script lang="ts">
  import { onMount } from "svelte";

  import { wrapDisplayCopy } from "../lib/displayCopy";
  import {
    canConnectSource,
    type SourceContentKind,
    type SourceDoorError
  } from "../lib/projectSources";
  import { settingsPageCopy } from "../lib/settingsPageCopy";

  export let submitting: boolean;
  export let error: SourceDoorError | null;
  export let onSubmit: (args: { address: string; token: string }) => void;
  export let onRetry: () => void;
  export let onClose: () => void;

  type DialogElement = HTMLElement & { open: boolean; showModal?: () => void; close?: () => void };

  let dialogElement: DialogElement;
  let addressInput: HTMLInputElement;
  let tokenInput: HTMLInputElement;
  let selectedKind: SourceContentKind = "items";
  let address = "";
  let token = "";

  $: canSubmit = !submitting && canConnectSource(selectedKind, address, token);

  onMount(() => {
    if (typeof dialogElement.showModal === "function") dialogElement.showModal();
    else dialogElement.open = true;
    addressInput.focus();
  });

  function dismiss(): void {
    if (submitting) return;
    dialogElement.close?.();
    onClose();
  }

  function submit(): void {
    if (!canSubmit) return;
    onSubmit({ address, token });
  }

  function chooseKind(kind: SourceContentKind): void {
    if (submitting) return;
    selectedKind = kind;
  }

  function handleCancel(event: Event): void {
    if (submitting) {
      event.preventDefault();
      return;
    }
    dismiss();
  }
</script>

<div class="sheet-positioner">
  <dialog
    bind:this={dialogElement}
    class="sheet"
    aria-labelledby="connect-source-title"
    aria-busy={submitting}
    oncancel={handleCancel}
  >
    <header>
      <h2 id="connect-source-title">{wrapDisplayCopy(settingsPageCopy.connectASource)}</h2>
    </header>

    <div class="fields">
      <div class="kind-field">
        <span id="connect-source-kind">{settingsPageCopy.sourceKind}</span>
        <div class="kind-chips" role="group" aria-labelledby="connect-source-kind">
          <button
            class="kind-chip"
            class:selected={selectedKind === "items"}
            type="button"
            aria-pressed={selectedKind === "items"}
            disabled={submitting}
            onclick={() => { chooseKind("items"); }}
          >{wrapDisplayCopy(settingsPageCopy.items)}</button>
          <button
            class="kind-chip"
            class:selected={selectedKind === "library"}
            type="button"
            aria-pressed={selectedKind === "library"}
            disabled={submitting}
            onclick={() => { chooseKind("library"); }}
          >{wrapDisplayCopy(settingsPageCopy.library)}</button>
        </div>
      </div>
      <label>
        {settingsPageCopy.where}
        <input
          bind:this={addressInput}
          bind:value={address}
          type="text"
          aria-label={settingsPageCopy.where}
          disabled={submitting}
        />
      </label>
      <label>
        {settingsPageCopy.token}
        <input
          bind:this={tokenInput}
          bind:value={token}
          type="password"
          autocomplete="off"
          spellcheck="false"
          aria-label={settingsPageCopy.token}
          disabled={submitting}
        />
      </label>
      <p class="note">{settingsPageCopy.neverShownAgain}</p>
    </div>

    {#if submitting}
      <p class="running" role="status">{settingsPageCopy.running}</p>
    {/if}
    {#if error !== null}
      <p class="error" role="alert">
        <span>{error.sentence}</span>
        <button
          class="next-step"
          type="button"
          disabled={submitting}
          onclick={() => {
            if (error.nextStep === settingsPageCopy.retry) {
              onRetry();
              return;
            }
            tokenInput.focus();
          }}
        >
          {wrapDisplayCopy(error.nextStep)}
        </button>
      </p>
    {/if}

    <footer>
      <button class="primary" type="button" disabled={!canSubmit} onclick={submit}>
        {wrapDisplayCopy(settingsPageCopy.connect)}
      </button>
      <button class="quiet" type="button" disabled={submitting} onclick={dismiss}>
        {wrapDisplayCopy(settingsPageCopy.cancel)}
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
  .fields { display: grid; gap: var(--space-3); margin: var(--space-4) 0; }
  label, .kind-field { display: grid; gap: var(--space-1); }
  input { min-height: var(--tap); }
  .kind-chips { display: flex; flex-wrap: wrap; gap: var(--space-2); }
  .kind-chip { min-height: var(--tap); padding: var(--space-2) var(--space-3); border: var(--edge) solid var(--line); border-radius: var(--r); background: transparent; color: var(--ink-dim); font-size: var(--text-2xs); font-weight: var(--weight-heavy); letter-spacing: var(--tracking-label); text-transform: uppercase; }
  .kind-chip.selected { background: var(--chip); color: var(--ink); border-color: var(--ink); }
  .note { margin: 0; color: var(--ink-dim); }
  .running { margin: 0 0 var(--space-3); color: var(--signal-live); }
  .error { display: flex; flex-wrap: wrap; align-items: center; gap: var(--space-2); margin: 0 0 var(--space-3); color: var(--signal-failure); }
  .next-step { min-height: var(--tap); border-color: transparent; background: transparent; color: var(--signal-failure); text-decoration: underline; text-underline-offset: var(--underline-offset); }
  @media (max-width: 390px) { .sheet { inset: auto 0 0 0; width: 100%; max-height: 85vh; border-radius: var(--r-lg) var(--r-lg) 0 0; } }
</style>
