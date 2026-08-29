<script lang="ts">
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import type { ProviderAccountRowView } from "../lib/providerAccounts";
  import { settingsPageCopy } from "../lib/settingsPageCopy";

  export let rows: ProviderAccountRowView[];
</script>

<section class="settings-block" aria-labelledby="accounts-title">
  <h2 id="accounts-title">{wrapDisplayCopy(settingsPageCopy.account)}</h2>
  {#if rows.length > 0}
    <ul class="provider-account-rows">
      {#each rows as row (`${row.providerId}:${row.profileId}`)}
        <li class="provider-account-row">
          <strong>{row.caption}</strong>
          <span>{row.secretPresentLabel}</span>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .settings-block { display: grid; align-content: start; gap: var(--space-3); min-width: 0; }
  h2 { margin: 0; color: var(--ink-dim); font-size: var(--text-2xs); font-weight: var(--weight-heavy); letter-spacing: var(--tracking-label); text-transform: uppercase; }
  .provider-account-rows { display: grid; gap: var(--space-2); margin: 0; padding: 0; list-style: none; }
  .provider-account-row {
    display: grid;
    gap: var(--space-2);
    background: var(--panel2);
    border: var(--edge) solid var(--line);
    border-radius: var(--r-lg);
    padding: var(--space-3);
    min-width: 0;
  }
  .provider-account-row strong { color: var(--ink); font-weight: var(--weight-strong); overflow-wrap: anywhere; }
  .provider-account-row span { color: var(--ink-dim); overflow-wrap: anywhere; }
</style>
