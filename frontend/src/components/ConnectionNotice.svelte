<script lang="ts">
  import { connectionState, restartNoticeCopy } from "../lib/connectionState";
  import { wrapDisplayCopy } from "../lib/displayCopy";
</script>

<!-- A healthy connection says nothing: only a lost one speaks, and in one
     calm line above every room, not a per-page error (#700, HEART's "One
     thing alive" -- this line names an outage, not work in progress, so its
     glyph never moves; it owns its own presentation entirely rather than
     borrowing the per-stream status pill's classes, so a future change to
     that pill can never again bleed into this line by accident). -->
{#if $connectionState === "reconnecting"}
  <p class="notice-banner" role="status">
    <span aria-hidden="true">↻</span>
    {wrapDisplayCopy(restartNoticeCopy)}
  </p>
{/if}

<style>
  .notice-banner {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-2);
    width: 100%;
    margin: 0;
    border-bottom: var(--edge) solid var(--line);
    padding: var(--space-1) var(--space-3);
    background: var(--panel2);
    color: var(--signal-attention);
    font-size: var(--text-xs);
    font-weight: var(--weight-heavy);
    text-align: center;
  }
</style>
