<script lang="ts">
  import { onMount } from "svelte";

  import type {
    AgentConfigurationRevisionListItem,
    CockpitApi,
    ObservedQueueItem,
    WorkflowRevisionDetail
  } from "../api/client";
  import { workflowStartCopy } from "../lib/catalogPageCopy";
  import { humanErrorMessage } from "../lib/humanRefusal";
  import {
    createRunId as makeRunId,
    requestedStartAgentBindings,
    startMutationV3,
    type MutationJournal
  } from "../lib/mutationJournal";
  import { namedAgentLabel } from "../lib/namedAgentChoice";
  import {
    classifyStartOrderSchema,
    summarizeOrderSchema,
    typeLabel,
    type OrderSchemaField,
    type OrderSchemaResource,
    type StartOrderSchemaShape
  } from "../lib/orderSchema";
  import { readEveryAgentConfiguration } from "../lib/runPages";
  import { agentRolesOf } from "../lib/savedWorkflows";
  import InfoHint from "./InfoHint.svelte";

  export let cockpitApi: CockpitApi;
  export let mutationJournal: MutationJournal;
  export let revision: WorkflowRevisionDetail;
  export let workflowName: string;
  export let navigate: (path: string) => void;
  export let createRunId: () => string = makeRunId;
  export let onClose: () => void;

  interface OrderDraft {
    readonly name: string;
    readonly schemaRef: string;
    readonly schemaRevision: string;
    resource: OrderSchemaResource | null;
    shape: StartOrderSchemaShape | null;
    values: Record<string, string>;
  }

  type DialogElement = HTMLElement & {
    open: boolean;
    showModal?: () => void;
    close?: () => void;
  };

  let configurations: readonly AgentConfigurationRevisionListItem[] = [];
  let observedQueueItems: readonly ObservedQueueItem[] = [];
  let selectedBindings: Record<string, string> = {};
  let orders: OrderDraft[] = [];
  let loading = true;
  let starting = false;
  let failure: string | null = null;
  let dialogElement: DialogElement;
  let closeButton: HTMLButtonElement;
  let opener: HTMLElement | null = null;

  $: roles = agentRolesOf(revision.graph);
  $: startableConfigurations = configurations.filter((configuration) => configuration.startable);
  $: roleSources = Object.fromEntries(
    roles.map((role) => [role, roleSource(selectedBindings[role])])
  ) as Record<string, string>;
  $: canStart =
    !loading &&
    !starting &&
    roles.every((role) => (selectedBindings[role]?.length ?? 0) > 0) &&
    orders.every(orderCanStart);
  $: observedItemsBySource = groupObservedItemsBySource(observedQueueItems);

  onMount(() => {
    const activeElement = globalThis.document.activeElement;
    opener = activeElement instanceof globalThis.HTMLElement ? activeElement : null;
    if (typeof dialogElement.showModal === "function") dialogElement.showModal();
    else dialogElement.open = true;
    closeButton.focus();
    void load();
  });

  async function load(): Promise<void> {
    loading = true;
    failure = null;
    try {
      const [configurationsPage, loadedOrders] = await Promise.all([
        readEveryAgentConfiguration((after) => cockpitApi.listAgentConfigurationRevisions(after)),
        Promise.all(declaredOrders().map(loadOrder))
      ]);
      if (!configurationsPage.complete) throw new Error("Agent configurations are incomplete.");
      configurations = configurationsPage.configurations;
      orders = loadedOrders;
      if (loadedOrders.some((order) => order.shape?.kind === "work_item")) {
        observedQueueItems = await readEveryObservedQueueItem();
      }
    } catch (error) {
      failure = humanErrorMessage(error, workflowStartCopy.sheetUnavailable);
    } finally {
      loading = false;
    }
  }

  async function readEveryObservedQueueItem(): Promise<readonly ObservedQueueItem[]> {
    const items: ObservedQueueItem[] = [];
    const seenCursors: string[] = [];
    let after: string | undefined;
    let nextAfter: string | null = "";
    while (nextAfter !== null) {
      const page = await cockpitApi.listObservedQueueItems(after);
      items.push(...page.items);
      nextAfter = page.next_after;
      if (nextAfter === null) continue;
      if (seenCursors.includes(nextAfter)) throw new Error("Observed queue items are incomplete.");
      seenCursors.push(nextAfter);
      after = nextAfter;
    }
    return items;
  }

  function declaredOrders(): OrderDraft[] {
    if (revision.graph.workflow_format_version !== 3) return [];
    return revision.graph.orders.map((order) => ({
      name: order.name,
      schemaRef: order.schema.ref,
      schemaRevision: order.schema.revision,
      resource: null,
      shape: null,
      values: {}
    }));
  }

  async function loadOrder(order: OrderDraft): Promise<OrderDraft> {
    const document = await cockpitApi.getSchemaRevision(order.schemaRevision);
    return {
      ...order,
      resource: { document, summary: summarizeOrderSchema(document) },
      shape: classifyStartOrderSchema(document, order.schemaRevision)
    };
  }

  function orderCanStart(order: OrderDraft): boolean {
    if (order.resource === null || order.shape === null || order.shape.kind === "unsupported") {
      return false;
    }
    if (order.shape.kind === "work_item") return (order.values.work_item?.length ?? 0) > 0;
    return requiredFieldsFilled(order);
  }

  function orderGroupLabel(order: OrderDraft): string {
    return order.shape?.kind === "work_item" ? workflowStartCopy.workItem : `Order ${order.name}`;
  }

  function requiredFieldsFilled(order: OrderDraft): boolean {
    return (order.resource?.summary.fields ?? [])
      .filter((field) => field.required)
      .every((field) => (order.values[field.name] ?? "").trim().length > 0);
  }

  function setOrderValue(orderName: string, field: string, value: string): void {
    orders = orders.map((order) =>
      order.name === orderName ? { ...order, values: { ...order.values, [field]: value } } : order
    );
  }

  function wireOrder(order: OrderDraft): { name: string; value: string } | { name: string; work_item: string } {
    if (order.shape?.kind === "work_item") {
      return { name: order.name, work_item: order.values.work_item ?? "" };
    }
    const fields = order.resource?.summary.fields ?? [];
    const value = Object.fromEntries(
      fields.flatMap((field) => {
        const typed = order.values[field.name] ?? "";
        return typed.length === 0 ? [] : [[field.name, typedValue(field, typed)]];
      })
    );
    return { name: order.name, value: JSON.stringify(value) };
  }

  function inputType(field: OrderSchemaField): "checkbox" | "number" | "text" {
    if (field.types?.includes("boolean")) return "checkbox";
    if (field.types?.includes("number") || field.types?.includes("integer")) return "number";
    return "text";
  }

  function typedValue(field: OrderSchemaField, value: string): boolean | number | string {
    if (field.types?.includes("boolean")) return value === "true";
    if (field.types?.includes("number") || field.types?.includes("integer")) return Number(value);
    return value;
  }

  function sourceOf(reference: string): string {
    const prefix = reference.split(":", 1)[0] ?? "";
    if (prefix === "gh") return "GitHub";
    if (prefix === "gl") return "GitLab";
    return prefix === "" ? workflowStartCopy.unknownSource : prefix;
  }

  function observedItemLabel(item: ObservedQueueItem): string {
    return `${sourceOf(item.tracker_item_reference)} · ${item.tracker_item_reference}`;
  }

  function groupObservedItemsBySource(
    items: readonly ObservedQueueItem[]
  ): ReadonlyArray<readonly [string, readonly ObservedQueueItem[]]> {
    const grouped: Array<[string, ObservedQueueItem[]]> = [];
    for (const item of items) {
      const source = sourceOf(item.tracker_item_reference);
      const group = grouped.find(([candidate]) => candidate === source);
      if (group === undefined) grouped.push([source, [item]]);
      else group[1].push(item);
    }
    return grouped;
  }

  function roleSource(selectedBinding: string | undefined): string {
    return selectedBinding === undefined
      ? workflowStartCopy.interimConfigurationNeeded
      : workflowStartCopy.interimConfigurationChosen;
  }

  function dismiss(): void {
    if (dialogElement.open && typeof dialogElement.close === "function") dialogElement.close();
    else dialogElement.open = false;
    onClose();
    globalThis.queueMicrotask(() => opener?.focus());
  }

  function containDialogFocus(event: KeyboardEvent): void {
    if (event.key === "Escape") {
      event.preventDefault();
      dismiss();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = Array.from(
      dialogElement.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), a[href]'
      )
    );
    const first = focusable[0];
    const last = focusable.at(-1);
    if (first === undefined || last === undefined) return;
    if (event.shiftKey && globalThis.document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && globalThis.document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  async function start(): Promise<void> {
    if (!canStart || revision.graph.workflow_format_version !== 3) return;
    starting = true;
    failure = null;
    const mutation = startMutationV3(
      createRunId(),
      revision.workflow_revision_hash,
      roles.map((role) => ({ role, agent_configuration_revision_hash: selectedBindings[role]! })),
      orders.map(wireOrder)
    );
    try {
      await mutationJournal.prepare(mutation);
      const result = await cockpitApi.start(mutation);
      const expected = requestedStartAgentBindings(mutation);
      const returned = "workflow_format_version" in result.value ? result.value.agent_bindings : null;
      if (
        expected === null ||
        returned === null ||
        expected.some(
          (binding) =>
            returned.find((candidate) => candidate.role === binding.role)
              ?.agent_configuration_revision_hash !== binding.agent_configuration_revision_hash
        )
      ) {
        throw new Error("The start response changed the selected roles.");
      }
      const resolved = await mutationJournal.resolve(mutation.mutation_id, {
        type: "start_response",
        status: result.status,
        target: mutation.target,
        request_body_base64: mutation.body_base64,
        run_id: result.value.run_id,
        public_run_reference: result.value.public_run_reference,
        workflow_revision_hash: result.value.workflow_revision_hash
      });
      if (!resolved) throw new Error("The start response did not prove the exact request.");
      navigate(`/atelier/runs/${result.value.public_run_reference}`);
    } catch (error) {
      failure = humanErrorMessage(error, workflowStartCopy.startUnavailable);
    } finally {
      starting = false;
    }
  }
</script>

<div class="sheet-positioner">
  <dialog
    bind:this={dialogElement}
    class="sheet"
    aria-labelledby="start-sheet-title"
    onkeydown={containDialogFocus}
    oncancel={dismiss}
  >
    <header>
      <h2 id="start-sheet-title">Start {workflowName}</h2>
    </header>
    {#if loading}
      <p>{workflowStartCopy.preparing}</p>
    {:else if failure !== null}
      <p class="failure" role="alert">{failure}</p>
      <button type="button" onclick={() => { void load(); }}>{workflowStartCopy.retry}</button>
    {:else}
      {#each orders as order (order.name)}
        <fieldset aria-label={orderGroupLabel(order)}>
          {#if order.shape?.kind !== "work_item"}
            <legend>{order.name}</legend>
            <p class="schema-summary">{order.schemaRef}@{order.schemaRevision}</p>
          {/if}
          {#if order.shape?.kind === "work_item"}
            <label>
              {workflowStartCopy.workItem}
              {#if observedItemsBySource.length === 0}
                <span class="degraded">
                  {workflowStartCopy.noSource}
                  <button
                    type="button"
                    class="link"
                    aria-label={workflowStartCopy.settings}
                    onclick={() => navigate("/atelier/settings")}
                  >
                    {workflowStartCopy.settings}
                  </button>
                </span>
              {:else}
                <select
                  aria-label={`${workflowStartCopy.workItem} for ${order.name}`}
                  value={order.values.work_item ?? ""}
                  disabled={starting}
                  onchange={(event) => setOrderValue(order.name, "work_item", event.currentTarget.value)}
                >
                  <option value="">{workflowStartCopy.choose}</option>
                  {#each observedItemsBySource as [source, items] (source)}
                    <optgroup label={source}>
                      {#each items as item (item.item_id)}
                        <option value={item.tracker_item_reference}>{observedItemLabel(item)}</option>
                      {/each}
                    </optgroup>
                  {/each}
                </select>
              {/if}
            </label>
          {:else if order.shape?.kind === "inline_object"}
            {#each order.resource?.summary.fields ?? [] as field (field.name)}
              <label>
                {field.name} ({typeLabel(field.types)}){field.required ? " *" : ""}
                {#if inputType(field) === "checkbox"}
                  <select
                    value={order.values[field.name] ?? ""}
                    disabled={starting}
                    onchange={(event) => setOrderValue(order.name, field.name, event.currentTarget.value)}
                  >
                    <option value="">{workflowStartCopy.choose}</option>
                    <option value="true">{workflowStartCopy.trueLabel}</option>
                    <option value="false">{workflowStartCopy.falseLabel}</option>
                  </select>
                {:else}
                  <input
                    type={inputType(field)}
                    value={order.values[field.name] ?? ""}
                    required={field.required}
                    disabled={starting}
                    oninput={(event) => setOrderValue(order.name, field.name, event.currentTarget.value)}
                  />
                {/if}
              </label>
            {/each}
          {:else}
            <p class="failure" role="alert">{order.shape?.reason ?? workflowStartCopy.orderUnavailable}</p>
          {/if}
        </fieldset>
      {/each}
      {#if roles.length > 0}
        <fieldset class="interim-configuration" aria-label={workflowStartCopy.interim}>
          <legend>
            {workflowStartCopy.interim}
            <InfoHint
              label={workflowStartCopy.interimConfigurationInfo}
              exact={workflowStartCopy.interimConfiguration}
              text={workflowStartCopy.info}
            />
          </legend>
          {#each roles as role (role)}
            <div class="role-row">
              <label>
                {role}
                <select
                  aria-label={`Configuration for ${role}`}
                  value={selectedBindings[role] ?? ""}
                  disabled={starting}
                  onchange={(event) => { selectedBindings = { ...selectedBindings, [role]: event.currentTarget.value }; }}
                >
                  <option value="">{workflowStartCopy.choose}</option>
                  {#each startableConfigurations as configuration (configuration.agent_configuration_revision_hash)}
                    <option value={configuration.agent_configuration_revision_hash}>{namedAgentLabel(configuration)}</option>
                  {/each}
                </select>
              </label>
              <p class="role-source">{roleSources[role]}</p>
            </div>
          {/each}
        </fieldset>
      {/if}
      {#if roles.length > 0 && startableConfigurations.length === 0}
        <p class="failure">{workflowStartCopy.noConfiguration}</p>
      {/if}
    {/if}
    <footer>
      {#if !loading && failure === null}
        <button type="button" class="primary" disabled={!canStart} onclick={start}>{workflowStartCopy.startRun}</button>
      {/if}
      <button bind:this={closeButton} type="button" class="quiet" disabled={starting} onclick={dismiss}>{workflowStartCopy.cancel}</button>
    </footer>
  </dialog>
</div>

<style>
  .sheet-positioner { position: fixed; inset: 0; z-index: 10; pointer-events: none; }
  .sheet { box-sizing: border-box; position: fixed; inset: var(--space-5) var(--space-5) auto auto; width: min(var(--dialog-width), calc(100% - (var(--space-5) * 2))); max-height: calc(100vh - (var(--space-5) * 2)); margin: 0; overflow-y: auto; padding: var(--space-5); background: var(--panel2); border: var(--edge) solid var(--ink); border-radius: var(--r-lg); box-shadow: var(--shadow-lift); pointer-events: auto; }
  .sheet::backdrop { background: color-mix(in srgb, var(--ground) 80%, transparent); }
  header, footer { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); }
  h2 { font-family: var(--serif); }
  fieldset, label { display: grid; gap: var(--space-1); margin: var(--space-4) 0; }
  input, select { min-height: var(--tap); font: inherit; }
  .failure { color: var(--signal-failure); }
  .degraded { display: flex; align-items: center; justify-content: space-between; gap: var(--space-2); border: 1px dashed var(--signal-attention); padding: var(--space-2); color: var(--signal-attention); }
  .link { min-height: var(--tap); border: 0; background: transparent; color: var(--ink); font: inherit; font-weight: var(--weight-strong); text-decoration: underline; }
  .interim-configuration { border-color: var(--ink); margin: var(--space-5) 0; padding: var(--space-3); }
  .interim-configuration legend { color: var(--ink); font-weight: var(--weight-strong); padding: 0 var(--space-1); }
  .role-row { margin: var(--space-4) 0; }
  .role-row label { margin: 0; }
  .role-source { color: var(--ink); font-size: var(--text-2xs); font-weight: var(--weight-strong); margin: var(--space-1) 0 0; }
  @media (max-width: 480px) { .sheet { inset: auto 0 0 0; width: 100%; height: auto; max-height: 85vh; border-radius: var(--r-lg) var(--r-lg) 0 0; } }
</style>
