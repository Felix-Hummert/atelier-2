<script lang="ts">
  import { connectionState, restartNoticeCopy } from "../lib/connectionState";
  import { wrapDisplayCopy } from "../lib/displayCopy";
</script>

<!-- A healthy connection says nothing: only a lost one speaks, and in one
     calm line above every room, not a per-page error (#700, HEART's "One
     thing alive" -- this line names an outage, not work in progress, so its
     glyph never moves; it owns its own presentation entirely rather than
     borrowing the per-stream status pill's classes, so a future change to
     that pill can never again bleed into this line by accident).

     `position: sticky` never worked here in the first place --
     `html,body{overflow-x:hidden}` breaks it for any descendant -- so the
     line is fixed to the viewport instead, over the rail and stage alike,
     never scrolled out of view while the outage lasts. A `position: fixed`
     element carries no height of its own for the layout below it to react
     to, so an identical, invisible copy stays in normal flow right where
     the visible one would otherwise sit: the same markup and styles give it
     the exact same height at any width or wrapped copy length -- no
     measuring, no separate number to keep in sync, no `ResizeObserver` -- and
     it appears and disappears in the same instant as the line itself, so
     there is no jump either way. It carries no `role` of its own so a role
     query still finds exactly the one real, visible banner. -->
{#if $connectionState === "reconnecting"}
  <p class="notice-spacer" aria-hidden="true">
    <span aria-hidden="true">↻</span>
    {wrapDisplayCopy(restartNoticeCopy)}
  </p>
  <p class="notice-banner" role="status">
    <span aria-hidden="true">↻</span>
    {wrapDisplayCopy(restartNoticeCopy)}
  </p>
{/if}

<style>
  .notice-banner,
  .notice-spacer {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-2);
    width: 100%;
    margin: 0;
    border-bottom: var(--edge) solid var(--line);
    padding: var(--space-1) var(--space-3);
    font-size: var(--text-xs);
    font-weight: var(--weight-heavy);
    text-align: center;
  }

  .notice-banner {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    z-index: 3;
    background: var(--panel2);
    color: var(--signal-attention);
  }

  .notice-spacer {
    visibility: hidden;
  }
</style>
