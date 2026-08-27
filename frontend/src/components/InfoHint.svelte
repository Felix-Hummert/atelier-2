<script lang="ts">
  /**
   * A quiet reveal beside a fact: either its exact bytes behind a shortened
   * reading, or a short prose explanation, shown on ask.
   *
   * The affordance is text, never a round icon button — a circle with a glyph
   * in it reads as a control that *starts* something rather than one that
   * *shows* something (operator, 23.08.). `text` is what the eye reads;
   * `label` stays the accessible name, so a reveal whose visible words are
   * the fact itself ("for 15 min") still announces what pressing it does.
   */
  export let label: string;
  export let exact: string | null = null;
  export let prose: string | null = null;
  export let text: string = label;
  export let pinToCard = false;

  let open = false;
  let pinnedOpen = false;
</script>

<span
  class="info-hint"
  style:position={pinToCard ? "static" : undefined}
  role="group"
  aria-label={label}
  onmouseenter={() => { open = true; }}
  onmouseleave={() => { if (!pinnedOpen) open = false; }}
  onfocusout={(event) => {
    if (!event.currentTarget.contains(event.relatedTarget as never)) {
      open = false;
      pinnedOpen = false;
    }
  }}
>
  <button
    class="reveal-affordance"
    type="button"
    aria-label={label}
    aria-expanded={open}
    onclick={() => {
      open = true;
      pinnedOpen = true;
    }}
  >{text}</button>
  {#if open}
    <span
      class="info-popover"
      role="status"
      style:right={pinToCard ? "auto" : undefined}
      style:left={pinToCard ? "var(--space-5)" : undefined}
    >
      {#if prose !== null}
        {prose}
      {:else if exact !== null}
        <code>{exact}</code>
      {/if}
    </span>
  {/if}
</span>
