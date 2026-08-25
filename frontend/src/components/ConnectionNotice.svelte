<script lang="ts">
  import { connectionState, restartNoticeCopy } from "../lib/connectionState";
  import { wrapDisplayCopy } from "../lib/displayCopy";
</script>

<!-- A healthy connection says nothing: only a lost one speaks, and in one
     calm line above every room, not a per-page error (#700, HEART's "One
     thing alive" -- stillness is the reward for what needs nothing). -->
{#if $connectionState === "reconnecting"}
  <p class="connection connection-reconnecting notice-banner" role="status">
    <span aria-hidden="true">↻</span>
    {wrapDisplayCopy(restartNoticeCopy)}
  </p>
{/if}

<style>
  .notice-banner {
    position: sticky;
    top: 0;
    z-index: 2;
    justify-content: center;
    width: 100%;
    margin: 0;
    border-width: 0 0 var(--edge) 0;
    border-radius: 0;
  }
</style>
