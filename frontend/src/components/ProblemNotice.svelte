<script lang="ts">
  import type { Problem } from "../api/client";

  export let title = "Request failed";
  export let message = "The request could not be completed.";
  export let problem: Problem | null = null;
</script>

<div class="notice" role="alert">
  <span class="notice-mark" aria-hidden="true">◇</span>
  <span>
    <strong>{problem?.title ?? title}</strong>
    <span>{problem?.detail ?? message}</span>
  </span>
  {#if problem !== null}<small>HTTP {problem.status}</small>{/if}
</div>
