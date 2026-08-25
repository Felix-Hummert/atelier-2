<script lang="ts">
  import type { InvalidField } from "../api/client";
  import {
    typeLabel,
    type OrderSchemaReadFailure,
    type OrderSchemaResource
  } from "../lib/orderSchema";
  import type { RetainedRead } from "../lib/readResource";
  import ReadState from "./ReadState.svelte";

  export let order: { name: string; schema: { ref: string; revision: string } };
  export let value: string;
  export let error: string | null;
  export let fieldErrors: readonly InvalidField[] = [];
  export let schemaRead: RetainedRead<OrderSchemaResource, OrderSchemaReadFailure>;
  export let busy: boolean;
  export let starting: boolean;
  export let onInput: (value: string) => void;
  export let onRetrySchema: () => void;

  $: resource = schemaRead.confirmed;
  $: singleField = resource?.summary.singleRequiredStringField ?? null;
  $: requiredFieldCount = resource?.summary.fields.filter((field) => field.required).length ?? 0;
  $: needsMultiFieldNote = resource !== null && singleField === null && requiredFieldCount > 1;
</script>

<article
  class="node-card binding-card"
  class:node-queued={!starting && error === null}
  class:node-working={starting && error === null}
  class:node-needs_you={error !== null}
  aria-label={`Order ${order.name}`}
>
  <header class="node-header">
    <span class="node-kind">Order</span><h3>{order.name}</h3>
  </header>
  <p class="muted"><code>{order.schema.ref}@{order.schema.revision}</code></p>
  <ReadState read={schemaRead} label={`schema for ${order.name}`} onRetry={onRetrySchema} />
  {#if resource !== null}
    {#if resource.summary.fields.length > 0}
      <section class="revision-orders" aria-label={`Fields of ${order.name}`}>
        <ul>
          {#each resource.summary.fields as field (field.name)}
            <li>
              <strong>{field.name}</strong>
              <span class="muted">{typeLabel(field.types)}</span>
              {#if field.required}<span class="muted">Required</span>{/if}
            </li>
          {/each}
        </ul>
      </section>
    {:else}
      <p class="muted">This schema names no fields; any JSON value it admits is accepted.</p>
    {/if}
  {/if}
  {#if needsMultiFieldNote}
    <p class="muted">
      This order's schema names more than one field. Fill it as JSON matching the fields above.
    </p>
  {/if}
  {#if singleField !== null}
    <label class="named-agent">Material
      <input
        type="text"
        value={value}
        oninput={(event) => onInput(event.currentTarget.value)}
        spellcheck="false"
        disabled={busy}
        aria-invalid={error !== null}
        aria-label={`Material ${order.name}`}
      />
    </label>
  {:else}
    <label class="named-agent">Material
      <textarea
        rows="6"
        value={value}
        oninput={(event) => onInput(event.currentTarget.value)}
        spellcheck="false"
        disabled={busy}
        aria-invalid={error !== null}
        aria-label={`Material ${order.name}`}
      ></textarea>
    </label>
  {/if}
  {#if error !== null}<p class="binding-error" role="alert">{error}</p>{/if}
  {#each fieldErrors as field (field.path)}
    <p class="binding-error" role="alert">{field.path} — {field.reason}</p>
  {/each}
</article>
