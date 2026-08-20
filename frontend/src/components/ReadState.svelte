<script lang="ts">
  import type { RetainedRead } from "../lib/readResource";

  type ReadStateFailure =
    | { kind: "unavailable"; title: string }
    | { kind: "incomplete"; title: string };

  export let read: RetainedRead<unknown, ReadStateFailure>;
  export let label: string;
  export let onRetry: () => void;

  $: loading = read.request.state === "loading";
  $: retrying = read.request.state === "failed";
  $: verb = retrying ? "Retry" : "Refresh";

  function activate(): void {
    if (!loading) onRetry();
  }
</script>

<div class="read-state">
  <div class="read-truth">
    {#if read.request.state === "loading"}
      <span class="read-progress" role="status">
        <span aria-hidden="true">↻</span>
        {read.confirmed === null ? "Looking…" : "Refreshing…"}
      </span>
    {:else if read.request.state === "failed"}
      <span class="read-failure" role="alert">
        <span class="read-mark" aria-hidden="true">◇</span>
        <strong>{read.request.failure.title}</strong>
      </span>
    {/if}
  </div>
  <button
    class="quiet"
    type="button"
    aria-label={`${verb} ${label}`}
    aria-disabled={loading}
    onclick={activate}
  >{verb}</button>
</div>

<style>
  .read-state {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 0.75rem;
    min-height: 3.25rem;
    margin-bottom: 1rem;
  }

  .read-truth {
    flex: 1;
  }

  .read-progress,
  .read-failure {
    display: inline-flex;
    align-items: center;
    gap: 0.55rem;
    color: var(--muted);
  }

  .read-failure {
    color: var(--warning);
  }

  .read-mark {
    font-size: 1.25rem;
  }

  button[aria-disabled="true"] {
    cursor: wait;
    border-color: transparent;
    color: var(--muted);
  }

  @media (max-width: 32rem) {
    .read-state {
      align-items: flex-start;
    }
  }
</style>
