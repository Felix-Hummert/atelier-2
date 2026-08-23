<script lang="ts">
  /**
   * A quiet reveal beside a fact: the exact bytes behind a shortened or
   * relative reading, shown on ask.
   *
   * The affordance is text, never a round icon button — a circle with a glyph
   * in it reads as a control that *starts* something rather than one that
   * *shows* something (operator, 23.08.). `text` is what the eye reads;
   * `label` stays the accessible name, so a reveal whose visible words are
   * the fact itself ("for 15 min") still announces what pressing it does.
   */
  export let label: string;
  export let exact: string;
  export let text: string = label;

  let open = false;
</script>

<span
  class="info-hint"
  role="group"
  aria-label={label}
  onmouseenter={() => { open = true; }}
  onmouseleave={() => { open = false; }}
  onfocusout={(event) => {
    if (!event.currentTarget.contains(event.relatedTarget as never)) {
      open = false;
    }
  }}
>
  <button
    class="reveal-affordance"
    type="button"
    aria-label={label}
    aria-expanded={open}
    onclick={() => { open = !open; }}
  >{text}</button>
  {#if open}
    <span class="info-popover" role="status"><code>{exact}</code></span>
  {/if}
</span>
