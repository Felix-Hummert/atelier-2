<script lang="ts">
  import { onMount } from "svelte";

  import type { AuthProfileRevision } from "../api/client";
  import { MODEL_ID, trimmedModelId } from "../lib/addModel";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import { providerAccount, settingsPageCopy } from "../lib/settingsPageCopy";

  export let options: AuthProfileRevision[];
  export let prefill: { authProfileRevisionHash: string; modelId: string } | null;
  export let submitting: boolean;
  export let onSubmit: (args: { profile: AuthProfileRevision; modelId: string }) => void;
  export let onClose: () => void;

  type DialogElement = HTMLElement & { open: boolean; showModal?: () => void; close?: () => void };

  let dialogElement: DialogElement;
  let modelInput: HTMLInputElement;
  let selectedHash = "";
  let modelId = "";

  $: selectedProfile = options.find(
    (option) => option.auth_profile_revision_hash === selectedHash
  ) ?? null;
  $: canSubmit = !submitting
    && options.length > 0
    && selectedProfile !== null
    && MODEL_ID.test(trimmedModelId(modelId));

  onMount(() => {
    if (typeof dialogElement.showModal === "function") dialogElement.showModal();
    else dialogElement.open = true;
    const prefillHash = prefill?.authProfileRevisionHash;
    const matching = prefillHash !== undefined
      && options.some((option) => option.auth_profile_revision_hash === prefillHash);
    selectedHash = matching && prefillHash !== undefined
      ? prefillHash
      : (options[0]?.auth_profile_revision_hash ?? "");
    if (prefill !== null) modelId = prefill.modelId;
    modelInput.focus();
  });

  function dismiss(): void {
    dialogElement.close?.();
    onClose();
  }

  function submit(): void {
    if (selectedProfile === null || !canSubmit) return;
    onSubmit({ profile: selectedProfile, modelId: trimmedModelId(modelId) });
  }
</script>

<div class="sheet-positioner">
  <dialog bind:this={dialogElement} class="sheet" aria-labelledby="add-model-title" oncancel={dismiss}>
    <header>
      <h2 id="add-model-title">{wrapDisplayCopy(settingsPageCopy.addModel)}</h2>
    </header>

    <div class="fields">
      <label>
        {settingsPageCopy.provider}
        <select
          bind:value={selectedHash}
          aria-label={settingsPageCopy.provider}
          disabled={submitting || options.length === 0}
        >
          {#each options as option (option.auth_profile_revision_hash)}
            <option value={option.auth_profile_revision_hash}>
              {providerAccount(option.provider_id, option.profile_id)}
            </option>
          {/each}
        </select>
      </label>
      <label>
        {settingsPageCopy.model}
        <input
          bind:this={modelInput}
          bind:value={modelId}
          type="text"
          aria-label={settingsPageCopy.model}
          disabled={submitting}
        />
      </label>
    </div>

    <footer>
      <button class="primary" type="button" disabled={!canSubmit} onclick={submit}>
        {wrapDisplayCopy(settingsPageCopy.add)}
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
  label { display: grid; gap: var(--space-1); }
  input, select { min-height: var(--tap); }
  @media (max-width: 390px) { .sheet { inset: auto 0 0 0; width: 100%; max-height: 85vh; border-radius: var(--r-lg) var(--r-lg) 0 0; } }
</style>
