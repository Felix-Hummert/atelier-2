<script lang="ts">
  /**
   * The one way back up (mockup v5 §03/§04: "← Board", "← Workflows").
   *
   * It replaces the trail that also repeated the page's own title: the title
   * is already the largest thing on the page, so a crumb saying it again is
   * the same fact twice and the lower-ranked copy falls (operator ruling
   * 23.08.). What is left is navigation only, carrying no statement of its
   * own.
   */
  export let label: string;
  export let path: string;
  export let navigate: (path: string) => void;
</script>

<nav class="back" aria-label="Where you are">
  <a href={path} onclick={(event) => { event.preventDefault(); navigate(path); }}>
    <span aria-hidden="true">←</span>{label}
  </a>
</nav>

<style>
  .back {
    display: flex;
  }

  a {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    min-height: 44px;
    color: var(--accent);
    font-size: var(--text-sm);
    font-weight: 700;
    text-decoration: none;
  }

  a:hover {
    text-decoration: underline;
  }
</style>
