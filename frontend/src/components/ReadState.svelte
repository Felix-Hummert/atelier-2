<script lang="ts">
  import type { RetainedRead } from "../lib/readResource";

  type ReadStateFailure =
    | { kind: "unavailable"; title: string }
    | { kind: "incomplete"; title: string };

  export let read: RetainedRead<unknown, ReadStateFailure>;
  export let label: string;
  export let onRetry: () => void;

  // The control mounts only in the failed state, so an operator's own retry
  // that fails again re-mounts a new button and would otherwise drop
  // keyboard focus into the void between the two failures. Refocusing it
  // exactly then -- never on a first, unprompted failure -- keeps the
  // keyboard journey continuous without stealing focus the operator never
  // asked this control for.
  let retriedByOperator = false;

  function activate(): void {
    retriedByOperator = true;
    onRetry();
  }

  function focusIfRetried(node: HTMLButtonElement): void {
    if (retriedByOperator) node.focus();
    retriedByOperator = false;
  }
</script>

<!--
  A confirmed read says nothing at all. The block used to hold its height on
  every surface whether or not it had anything to report, which put a band of
  empty space between a page's title and its content forever after the read
  landed (operator ruling 23.08.: no element that carries no statement).
-->
{#if read.request.state !== "idle"}
  <div class="read-state">
    <div class="read-truth">
      {#if read.request.state === "loading"}
        <span class="read-progress" role="status">
          <span aria-hidden="true">↻</span>
          {read.confirmed === null ? "Looking…" : "Refreshing…"}
        </span>
      {:else}
        <span class="read-failure" role="alert">
          <span class="read-mark" aria-hidden="true">◇</span>
          <strong>{read.request.failure.title}</strong>
        </span>
      {/if}
    </div>
    {#if read.request.state === "failed"}
      <button
        class="quiet"
        type="button"
        aria-label={`Retry ${label}`}
        onclick={activate}
        use:focusIfRetried
      >Retry</button>
    {/if}
  </div>
{/if}

<style>
  .read-state {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 0.75rem;
    min-height: 3.25rem;
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

  @media (max-width: 32rem) {
    .read-state {
      align-items: flex-start;
    }
  }
</style>
