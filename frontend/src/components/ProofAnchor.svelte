<script lang="ts">
  export let label: string;
  export let seals: string;
  export let value: string;
  /** Named chip when the human name is not already beside the control. */
  export let compact = false;

  let open = false;
  let copied = false;

  async function activate(): Promise<void> {
    open = true;
    copied = false;
    const clipboard = globalThis.navigator.clipboard;
    if (clipboard === undefined) {
      return;
    }
    try {
      await clipboard.writeText(value);
      copied = true;
    } catch {
      // Reveal still answers the ask when the clipboard is missing or refused.
      copied = false;
    }
  }
</script>

<span
  class="proof-anchor"
  role="group"
  aria-label={label}
  onfocusout={(event) => {
    if (!event.currentTarget.contains(event.relatedTarget as never)) {
      open = false;
      copied = false;
    }
  }}
>
  <button
    class={compact ? "proof-compact" : "info-button"}
    type="button"
    aria-label={label}
    aria-expanded={open}
    onclick={() => {
      void activate();
    }}
  >{compact ? label : "ⓘ"}</button>
  {#if open}
    <span class="info-popover" role="status">
      <p class="proof-seals">Seals {seals}.</p>
      <code>{value}</code>
      {#if copied}<p class="proof-copied">Copied</p>{/if}
    </span>
  {/if}
</span>
