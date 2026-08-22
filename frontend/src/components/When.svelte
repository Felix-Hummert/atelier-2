<script lang="ts">
  import { studioQuestions } from "../lib/studioQuestions";
  import { ageLabel, exactLocal } from "../lib/when";
  import InfoHint from "./InfoHint.svelte";

  export let startedAt: string | null;
  export let endedAt: string | null = null;
  export let now: Date = new Date();
  export let kind: "ago" | "for" | "duration" = "ago";

  $: label =
    startedAt === null
      ? null
      : ageLabel(startedAt, now, kind, endedAt === null ? undefined : endedAt);
  $: exact =
    startedAt === null
      ? null
      : endedAt === null
        ? exactLocal(startedAt)
        : `${exactLocal(startedAt)} → ${exactLocal(endedAt)}`;
</script>

{#if label !== null && exact !== null}
  <span class="when">
    <span>{label}</span>
    <InfoHint label={studioQuestions.lastLandingTime.hintLabel} {exact} />
  </span>
{/if}

<style>
  .when { display: inline-flex; align-items: baseline; gap: 0.25rem; }
</style>
