<script lang="ts">
  export let kind: "workflow" | "agent";
  export let title: string;
  export let ariaLabel: string;
  export let description: string;
  export let provenance: readonly string[];
  export let provider: string | null = null;
  export let href: string | null = null;
  export let status: { label: string; description: string; dashed: boolean } | null = null;
  export let onOpen: ((path: string) => void) | null = null;

  const glyphByKind = {
    workflow: "⧉",
    agent: "◯"
  } as const;

  $: glyph = glyphByKind[kind];
  $: providerMark = provider?.slice(0, 1).toUpperCase() ?? null;
</script>

<li class="catalog-tile" class:catalog-tile-link={href !== null}>
  {#if href !== null}
    <a
      class="tile-door"
      {href}
      aria-label={ariaLabel}
      onclick={(event) => {
        event.preventDefault();
        onOpen?.(href);
      }}
    >
      <span class="tile-name"><span class="tile-glyph" aria-hidden="true">{glyph}</span>{title}</span>
      <span class="tile-description">{description}</span>
      <span class="tile-details">
        {#if provider !== null && providerMark !== null}
          <span class="provider-mark" aria-label={provider} title={provider}>{providerMark}</span>
        {/if}
        {#each provenance as source (source)}
          <span class="tile-pill">{source}</span>
        {/each}
        {#if status !== null}
          <span
            class="tile-pill tile-status"
            class:tile-status-dashed={status.dashed}
            title={status.description}
          >{status.label}</span>
        {/if}
      </span>
    </a>
  {:else}
    <div class="tile-static">
      <span class="tile-name"><span class="tile-glyph" aria-hidden="true">{glyph}</span>{title}</span>
      <span class="tile-description">{description}</span>
      <span class="tile-details">
        {#if provider !== null && providerMark !== null}
          <span class="provider-mark" aria-label={provider} title={provider}>{providerMark}</span>
        {/if}
        {#each provenance as source (source)}
          <span class="tile-pill">{source}</span>
        {/each}
        {#if status !== null}
          <span
            class="tile-pill tile-status"
            class:tile-status-dashed={status.dashed}
            title={status.description}
          >{status.label}</span>
        {/if}
      </span>
    </div>
  {/if}
</li>

<style>
  .catalog-tile {
    min-width: 0;
    border: var(--edge) solid var(--line);
    border-radius: var(--r-lg);
    background: var(--panel2);
  }

  .catalog-tile-link:hover,
  .catalog-tile-link:focus-within {
    border-color: var(--ink-dim);
  }

  .tile-door,
  .tile-static {
    display: grid;
    gap: var(--space-2);
    min-height: 100%;
    padding: var(--space-3) var(--space-4);
    color: inherit;
    text-decoration: none;
  }

  .tile-name {
    display: flex;
    align-items: baseline;
    gap: var(--space-2);
    font-size: var(--text-sm);
    font-weight: var(--weight-strong);
  }

  .tile-glyph {
    flex: none;
    color: var(--ink-dim);
    font-weight: var(--weight-medium);
  }

  .tile-description {
    color: var(--ink-dim);
    font-size: var(--text-xs);
  }

  .tile-details {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
    margin-top: auto;
  }

  .provider-mark {
    display: inline-grid;
    place-items: center;
    width: var(--space-5);
    height: var(--space-5);
    border: var(--edge) solid var(--ink-dim);
    border-radius: var(--r-pill);
    color: var(--ink-dim);
    font-size: var(--text-2xs);
    font-weight: var(--weight-heavy);
  }

  .tile-pill {
    border: var(--edge) solid var(--line);
    border-radius: var(--r-pill);
    padding: 0 var(--space-2);
    color: var(--ink-dim);
    background: var(--chip);
    font-size: var(--text-2xs);
    line-height: 1.65;
  }

  .tile-status {
    border-color: var(--signal-attention-mark);
    color: var(--signal-attention);
    background: transparent;
  }

  .tile-status-dashed {
    border-style: dashed;
    border-color: var(--line);
    color: var(--ink-dim);
  }
</style>
