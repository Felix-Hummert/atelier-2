<script lang="ts">
  export let label: string;
  export let exact: string;

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
    class="info-button"
    type="button"
    aria-label={label}
    aria-expanded={open}
    onclick={() => { open = !open; }}
  >ⓘ</button>
  {#if open}
    <span class="info-popover" role="status"><code>{exact}</code></span>
  {/if}
</span>
