<script lang="ts">
  import { tick } from "svelte";

  import { decodeCanonicalBase64, type Run } from "../api/client";
  import { decisionStatusCopy } from "../lib/decisionStatusCopy";
  import {
    reconciliationCommand,
    type ReconciliationDeterminationInput,
    type ReconciliationMutation
  } from "../lib/mutationJournal";
  import { PRODUCT_NAME } from "../lib/productName";
  import { byteCountCopy, runPageCopy } from "../lib/runPageCopy";
  import InfoHint from "./InfoHint.svelte";

  type WaitingReconciliation = Extract<Run["waiting"], { type: "WAITING_RECONCILIATION" }>;

  export let waiting: WaitingReconciliation;
  export let pending: ReconciliationMutation | null;
  export let accepted = false;
  export let busy = false;
  export let failureMessage: string | null = null;
  export let onResolve: (
    actor: string,
    evidence: string,
    determination: ReconciliationDeterminationInput
  ) => Promise<void>;
  export let onRetry: () => void;
  export let onDiscard: () => void;

  let actor = "";
  let evidence = "";
  let determination: "found" | "absent" = "found";
  let effectId = "";
  let resultBase64 = "";
  let validationMessage: string | null = null;
  let absenceDialog = false;
  let reviewButton: { focus(): void };
  let dialogElement: globalThis.HTMLDialogElement;
  let cancelButton: HTMLButtonElement;
  let executeButton: HTMLButtonElement;
  let actorInput: { focus(): void };
  let retryButton: { focus(): void };
  let statusHeading: { focus(): void };

  $: localCommand = pending === null ? null : reconciliationCommand(pending);
  $: visibleCommand = waiting.pending_command ?? localCommand;
  $: pendingResult = visibleCommand?.determination.type === "operator_found"
    ? visibleResult(visibleCommand.determination.result_base64)
    : null;

  export function focusInput(): void {
    actorInput?.focus();
  }

  export function focusRetry(): void {
    retryButton?.focus();
  }

  export function focusStatus(): void {
    statusHeading?.focus();
  }

  async function submitFound(event: Event): Promise<void> {
    event.preventDefault();
    validationMessage = validate(true);
    if (validationMessage !== null) return;
    await onResolve(actor, evidence, {
      type: "operator_found",
      effect_id: effectId,
      result_base64: resultBase64
    });
  }

  async function reviewAbsence(): Promise<void> {
    validationMessage = validate(false);
    if (validationMessage !== null) return;
    absenceDialog = true;
    await tick();
    dialogElement.showModal();
    cancelButton.focus();
  }

  async function closeAbsenceDialog(): Promise<void> {
    dialogElement.close();
    absenceDialog = false;
    await tick();
    reviewButton.focus();
  }

  async function executeAbsence(): Promise<void> {
    dialogElement.close();
    absenceDialog = false;
    await onResolve(actor, evidence, { type: "operator_authoritative_absence" });
  }

  function handleDialogCancel(event: Event): void {
    event.preventDefault();
    void closeAbsenceDialog();
  }

  function containDialogFocus(event: KeyboardEvent): void {
    if (event.key !== "Tab") return;
    if (event.shiftKey && globalThis.document.activeElement === cancelButton) {
      event.preventDefault();
      executeButton.focus();
    } else if (!event.shiftKey && globalThis.document.activeElement === executeButton) {
      event.preventDefault();
      cancelButton.focus();
    }
  }

  function validate(found: boolean): string | null {
    if (actor.trim().length === 0) return runPageCopy.reconciliation.nameActor;
    if (evidence.trim().length === 0) return runPageCopy.reconciliation.recordEvidence;
    if (found && effectId.trim().length === 0) return runPageCopy.reconciliation.nameEffectId;
    if (found && decodeCanonicalBase64(resultBase64) === null) {
      return runPageCopy.reconciliation.canonicalResult;
    }
    return null;
  }

  function visibleResult(encoded: string): string {
    const bytes = decodeCanonicalBase64(encoded);
    if (bytes === null) return runPageCopy.reconciliation.invalidResult;
    if (bytes.byteLength === 0) return runPageCopy.reconciliation.emptyResult;
    try {
      return new globalThis.TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch {
      return encoded;
    }
  }
</script>

<section
  class="human-action reconciliation-action"
  class:human-action-working={visibleCommand !== null || busy}
  aria-labelledby="reconciliation-action-title"
>
  <div class="human-action-header">
    <p class="eyebrow">{runPageCopy.reconciliation.title}</p>
    <span
      class="human-action-shape"
      class:human-action-shape-working={visibleCommand !== null || busy}
      class:human-action-shape-needs={visibleCommand === null && !busy}
      aria-hidden="true"
    >{visibleCommand === null && !busy ? "!" : "▲"}</span>
  </div>

  {#if visibleCommand !== null || busy}
    <h2 id="reconciliation-action-title" tabindex="-1" bind:this={statusHeading}>
      {busy ? runPageCopy.reconciliation.sending : accepted || waiting.pending_command !== null ? runPageCopy.reconciliation.pending : runPageCopy.reconciliation.uncertain}
    </h2>
    {#if failureMessage !== null}
      <div class="wait-alert" role="alert" aria-label={decisionStatusCopy.sendUncertain}>
        <span class="wait-alert-shape" aria-hidden="true">?</span>
        <span><strong>{decisionStatusCopy.sendUncertain}</strong><small>{failureMessage}</small></span>
      </div>
    {/if}
    {#if visibleCommand !== null}
      <dl class="decision-summary">
        <div><dt>{runPageCopy.reconciliation.actor}</dt><dd>{visibleCommand.actor}</dd></div>
        <div><dt>{runPageCopy.reconciliation.evidence}</dt><dd>{visibleCommand.evidence}</dd></div>
        <div><dt>{runPageCopy.reconciliation.command}</dt><dd><code>{visibleCommand.command_id}</code></dd></div>
        {#if visibleCommand.determination.type === "operator_found"}
          <div><dt>{runPageCopy.reconciliation.effectId}</dt><dd>{visibleCommand.determination.effect_id}</dd></div>
          <div><dt>{runPageCopy.reconciliation.result}</dt><dd class:empty-value={pendingResult === runPageCopy.reconciliation.emptyResult}>{pendingResult}</dd></div>
        {:else}
          <div><dt>{runPageCopy.reconciliation.decision}</dt><dd>{runPageCopy.reconciliation.authoritativeAbsence}</dd></div>
        {/if}
      </dl>
    {/if}
    {#if visibleCommand !== null && pending !== null && !accepted && waiting.pending_command === null && !busy}
      <div class="actions">
        <button type="button" onclick={onRetry} bind:this={retryButton}>{runPageCopy.retry}</button>
        <button class="quiet" type="button" onclick={onDiscard}>{runPageCopy.discard}</button>
      </div>
    {/if}
  {:else}
    <h2 id="reconciliation-action-title">{runPageCopy.reconciliation.decisionNeeded}</h2>
    <dl class="request-summary">
      <div><dt>{runPageCopy.reconciliation.effect}</dt><dd>{waiting.logical_effect_key}</dd></div>
      <div><dt>{runPageCopy.reconciliation.hash}</dt><dd><code>{waiting.request_hash}</code></dd></div>
      <div><dt>{runPageCopy.reconciliation.version}</dt><dd>{waiting.intent_state_version}</dd></div>
      <div>
        <dt>{runPageCopy.request}</dt>
        <dd>
          <span>{byteCountCopy(decodeCanonicalBase64(waiting.request_base64)?.byteLength ?? 0)}</span>
          <InfoHint label={runPageCopy.reconciliation.requestInfo} exact={waiting.request_base64} />
        </dd>
      </div>
    </dl>
    <form class="reconciliation-form" onsubmit={submitFound} novalidate>
      <label for="reconcile-actor">{runPageCopy.reconciliation.actor}</label>
      <input id="reconcile-actor" type="text" autocomplete="name" bind:value={actor} bind:this={actorInput} />
      <label for="reconcile-evidence">{runPageCopy.reconciliation.evidence}</label>
      <textarea id="reconcile-evidence" rows="3" bind:value={evidence}></textarea>
      <fieldset class="determination-picker">
        <legend>{runPageCopy.reconciliation.decision}</legend>
        <label><input type="radio" name="reconciliation-determination" bind:group={determination} value="found" /> {runPageCopy.reconciliation.found}</label>
        <label><input type="radio" name="reconciliation-determination" bind:group={determination} value="absent" /> {runPageCopy.reconciliation.absent}</label>
      </fieldset>
      {#if determination === "found"}
        <label for="reconcile-effect">{runPageCopy.reconciliation.effectId}</label>
        <input id="reconcile-effect" type="text" autocomplete="off" bind:value={effectId} />
        <label for="reconcile-result">{runPageCopy.reconciliation.exactResult}</label>
        <textarea id="reconcile-result" rows="3" spellcheck="false" bind:value={resultBase64}></textarea>
      {/if}
      {#if validationMessage !== null}
        <p class="field-error" role="alert">{validationMessage}</p>
      {/if}
      {#if determination === "found"}
        <button class="primary" type="submit" disabled={busy}>{runPageCopy.reconciliation.resolve}</button>
      {:else}
        <button
          class="primary"
          type="button"
          disabled={busy}
          bind:this={reviewButton}
          onclick={() => { void reviewAbsence(); }}
        >{runPageCopy.reconciliation.review}</button>
      {/if}
    </form>
  {/if}
</section>

{#if absenceDialog}
  <dialog
    class="dialog"
    aria-labelledby="absence-title"
    bind:this={dialogElement}
    oncancel={handleDialogCancel}
    onkeydown={containDialogFocus}
  >
    <h2 id="absence-title">{runPageCopy.reconciliation.executeQuestion}</h2>
    <p>{runPageCopy.reconciliation.executeOnce(PRODUCT_NAME)}</p>
    <div class="dialog-actions">
      <button
        class="quiet"
        type="button"
        bind:this={cancelButton}
        onclick={() => { void closeAbsenceDialog(); }}
      >{runPageCopy.reconciliation.cancel}</button>
      <button
        class="primary"
        type="button"
        bind:this={executeButton}
        onclick={() => { void executeAbsence(); }}
      >{runPageCopy.reconciliation.execute}</button>
    </div>
  </dialog>
{/if}
