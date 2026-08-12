<script lang="ts">
  import type { WaitMutation } from "../lib/mutationJournal";

  export let pending: WaitMutation | null;
  export let pendingAnswer: string | null;
  export let accepted = false;
  export let busy = false;
  export let validationMessage: string | null = null;
  export let onAnswer: (answer: string) => void;
  export let onRetry: () => void;
  export let onDiscard: () => void;

  let answer = "";

  function submit(event: Event): void {
    event.preventDefault();
    onAnswer(answer);
  }
</script>

<section class="human-action" class:human-action-working={pending !== null} aria-labelledby="wait-action-title">
  <p class="eyebrow">Wait node</p>
  {#if pending !== null}
    <h2 id="wait-action-title">{busy ? "Sending answer" : accepted ? "Answer pending" : "Answer uncertain"}</h2>
    <output class="exact-answer" aria-label="Exact answer">{pendingAnswer}</output>
    {#if !accepted && !busy}
      <div class="actions">
        <button type="button" disabled={busy} onclick={onRetry}>Retry</button>
        <button class="quiet" type="button" disabled={busy} onclick={onDiscard}>Discard</button>
      </div>
    {/if}
  {:else}
    <h2 id="wait-action-title">Answer needed</h2>
    <form class="wait-form" onsubmit={submit} novalidate>
      <label for="wait-answer">Integer answer</label>
      <input
        id="wait-answer"
        type="text"
        inputmode="numeric"
        autocomplete="off"
        spellcheck="false"
        bind:value={answer}
        aria-describedby={validationMessage === null ? undefined : "wait-validation"}
        aria-invalid={validationMessage === null ? undefined : "true"}
      />
      {#if validationMessage !== null}
        <p id="wait-validation" class="field-error" role="alert">{validationMessage}</p>
      {/if}
      <button class="primary" type="submit" disabled={busy}>Answer</button>
    </form>
  {/if}
</section>
