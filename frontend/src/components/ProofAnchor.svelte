<script lang="ts">
  export let label: string;
  export let seals: string;
  export let value: string;

  let open = false;
  let copied = false;

  async function copy(): Promise<void> {
    await globalThis.navigator.clipboard.writeText(value);
    copied = true;
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
    class="info-button"
    type="button"
    aria-label={label}
    aria-expanded={open}
    onclick={() => {
      open = !open;
      copied = false;
    }}
  >ⓘ</button>
  {#if open}
    <span class="info-popover" role="status">
      <p class="proof-seals">Seals {seals}.</p>
      <code>{value}</code>
      <button type="button" class="quiet" onclick={copy}>{copied ? "Copied" : "Copy"}</button>
    </span>
  {/if}
</span>
