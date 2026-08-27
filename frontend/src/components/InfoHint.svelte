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
  let closeTimeout: number | null = null;

  function closeAfterLeavingHint(): void {
    if (closeTimeout !== null) window.clearTimeout(closeTimeout);
    closeTimeout = window.setTimeout(() => {
      open = false;
      pinnedOpen = false;
      closeTimeout = null;
    });
  }
</script>

<span
  class:pin-to-card={pinToCard}
  class="info-hint"
  role="group"
  aria-label={label}
  onmouseenter={() => { open = true; }}
  onmouseleave={() => { if (!pinnedOpen) open = false; }}
  onfocusout={(event) => {
    if (!event.currentTarget.contains(event.relatedTarget as never)) {
      closeAfterLeavingHint();
    }
  }}
>
  <button
    class="reveal-affordance"
    type="button"
    aria-label={label}
    aria-expanded={open}
    onclick={() => {
      if (closeTimeout !== null) window.clearTimeout(closeTimeout);
      open = true;
      pinnedOpen = true;
    }}
  >{text}</button>
  {#if open}
    <span class="info-popover" role="status">
      {#if prose !== null}
        {prose}
      {:else if exact !== null}
        <code>{exact}</code>
      {/if}
    </span>
  {/if}
</span>

<style>
  /* The hint itself stays positioned. Card-bound prose joins the card's flow,
     so at phone width it fits inside the card and moves its doors down instead
     of overlaying them. */
  .pin-to-card {
    display: grid;
    justify-self: stretch;
    justify-items: start;
  }

  .pin-to-card .info-popover {
    position: static;
    width: min(var(--popover-width), calc(100vw - var(--space-6)));
    max-width: 100%;
  }
</style>
