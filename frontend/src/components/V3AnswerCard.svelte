<script lang="ts">
  import type { WaitMutation } from "../lib/mutationJournal";

  export let nodeId: string;
  export let question: string | null;
  export let questionMissing: boolean;
  export let questionFailed = false;
  export let pending: WaitMutation | null;
  export let pendingAnswer: string | null;
  export let accepted = false;
  export let busy = false;
  export let validationMessage: string | null = null;
  export let failureMessage: string | null = null;
  export let onAnswer: (answer: string) => void;
  export let onRetry: () => void;
  export let onDiscard: () => void;

  let answer = "";
  let answerInput: { focus(): void };
  let retryButton: { focus(): void };
  let statusHeading: { focus(): void };

  export function focusInput(): void {
    answerInput?.focus();
  }

  export function focusRetry(): void {
    retryButton?.focus();
  }

  export function focusStatus(): void {
    statusHeading?.focus();
  }

  function submit(event: Event): void {
    event.preventDefault();
    onAnswer(answer);
  }
</script>

<section
  class="human-action"
  class:human-action-working={pending !== null}
  aria-labelledby="v3-wait-action-title"
>
  <div class="human-action-header">
    <p class="eyebrow">Wait {nodeId}</p>
    <span
      class="human-action-shape"
      class:human-action-shape-working={pending !== null}
      class:human-action-shape-needs={pending === null}
      aria-hidden="true"
    >{pending === null ? "!" : "▲"}</span>
  </div>
  {#if pending !== null}
    <h2 id="v3-wait-action-title" tabindex="-1" bind:this={statusHeading}>{busy ? "Sending answer" : accepted ? "Answer pending" : "Answer uncertain"}</h2>
    {#if failureMessage !== null}
      <div class="wait-alert" role="alert" aria-label="Send uncertain">
        <span class="wait-alert-shape" aria-hidden="true">?</span>
        <span><strong>Send uncertain</strong><small>{failureMessage}</small></span>
      </div>
    {/if}
    <output class="exact-answer" aria-label="Exact answer">{pendingAnswer}</output>
    {#if !accepted && !busy}
      <div class="actions">
        <button type="button" disabled={busy} onclick={onRetry} bind:this={retryButton}>Retry</button>
        <button class="quiet" type="button" disabled={busy} onclick={onDiscard}>Discard</button>
      </div>
    {/if}
  {:else}
    <h2 id="v3-wait-action-title">Answer needed</h2>
    {#if question !== null}
      <p class="wait-question">{question}</p>
    {:else if questionMissing}
      <p class="muted">This wait node carries no question.</p>
    {:else if !questionFailed}
      <p class="muted" role="status">Looking…</p>
    {/if}
    <form class="wait-form" onsubmit={submit} novalidate>
      <label for="v3-wait-answer">Answer</label>
      <textarea
        id="v3-wait-answer"
        rows="6"
        spellcheck="false"
        bind:value={answer}
        aria-describedby={validationMessage === null ? undefined : "v3-wait-validation"}
        aria-invalid={validationMessage === null ? undefined : "true"}
        bind:this={answerInput}
      ></textarea>
      {#if failureMessage !== null}
        <div class="wait-alert" role="alert" aria-label="Send failed">
          <span class="wait-alert-shape" aria-hidden="true">!</span>
          <span><strong>Send failed</strong><small>{failureMessage}</small></span>
        </div>
      {/if}
      {#if validationMessage !== null}
        <p id="v3-wait-validation" class="field-error" role="alert">{validationMessage}</p>
      {/if}
      <button class="primary" type="submit" disabled={busy}>Answer</button>
    </form>
  {/if}
</section>
