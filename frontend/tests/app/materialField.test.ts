import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import type { CockpitApi, RunV3, WorkflowRevisionDetail } from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { WORK_ITEM_ORDER_SCHEMA_REVISION } from "../../src/lib/orderSchema";
import { cockpitApiStub } from "../support/cockpitApi";
import { cancellableBlock } from "../support/runV3";

const revisionHash = "a".repeat(64);
const configurationHash = "b".repeat(64);
const publicReference = "run1.cnVuLW9yZGVy";
const workflowName = "Cook to order";

const portionsOrder = {
  name: "portions",
  schema: { ref: "portions-schema", revision: "schema-portions" }
};

const portionsSchema = {
  type: "object",
  required: ["portions"],
  additionalProperties: false,
  properties: { portions: { type: "integer", minimum: 0 } }
};

const workItemOrder = {
  name: "work",
  schema: { ref: "work-item-schema", revision: WORK_ITEM_ORDER_SCHEMA_REVISION }
};

const workItemSchema = {
  title: "work item",
  type: "object",
  additionalProperties: false,
  required: ["body", "change_marker", "digest", "kind", "observed_at", "reference"],
  properties: {
    body: { type: "string" },
    change_marker: { type: "string" },
    digest: { type: "string" },
    kind: { type: "string" },
    observed_at: { type: "string" },
    reference: { type: "string" }
  }
};

function detail(orders = [portionsOrder]): WorkflowRevisionDetail {
  return {
    workflow_revision_hash: revisionHash,
    document_base64: "YQ==",
    graph: {
      workflow_format_version: 3,
      executable: true,
      not_executable_reason: null,
      node_count: 1,
      agent_roles: ["cook"],
      orders,
      wait_answer_schemas: [],
      node_previews: [],
      loops: [],
      name: workflowName,
      description: null
    }
  };
}

function startedRun(): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "run-order",
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    agent_binding_set_hash: "c".repeat(64),
    run_configuration_revision_hash: "d".repeat(64),
    agent_bindings: [{
      role: "cook",
      agent_configuration_revision_hash: configurationHash,
      auth_profile_revision_hash: "f".repeat(64),
      profile_id: "test",
      revision_number: 1,
      provider_id: "test",
      auth_mode: "subscription",
      model: "cook-model",
      executor_revision: "immediate/v1"
    }],
    orders: [],
    state_version: 1,
    state: "STARTED",
    current_node_id: "cook",
    node_rail: [{ node_id: "cook", state: "working", attempt: null }],
    cancellation: cancellableBlock(),
    terminal_hash: null,
    latest_event_cursor: null
  };
}

function api(overrides: Partial<CockpitApi> = {}): CockpitApi {
  return cockpitApiStub({
    listWorkflowRevisions: vi.fn(async () => ({
      items: [{
        workflow_revision_hash: revisionHash,
        workflow_format_version: 3 as const,
        executable: true,
        not_executable_reason: null,
        name: workflowName,
        description: null
      }],
      next_after_revision_hash: null
    })),
    getRevisionByName: vi.fn(async () => ({
      display_name: workflowName,
      lineage_id: "e".repeat(64),
      workflow_revision_hash: revisionHash,
      revision_number: 1
    })),
    getWorkflowRevision: vi.fn(async () => detail()),
    getSchemaRevision: vi.fn(async () => portionsSchema),
    listAgentConfigurationRevisions: vi.fn(async () => ({
      items: [{
        agent_configuration_revision_hash: configurationHash,
        provider_id: "test",
        model: "cook-model",
        auth_mode: "subscription" as const,
        auth_profile_revision_hash: "f".repeat(64),
        executor_revision: "immediate/v1",
        requested_capability: "headless" as const,
        startable: true,
        not_startable_reason: null
      }],
      next_after_revision_hash: null
    })),
    start: vi.fn(async () => ({ status: 201, value: startedRun() })),
    getRun: vi.fn(async () => startedRun()),
    ...overrides
  });
}

async function openStart(cockpitApi: CockpitApi): Promise<void> {
  window.history.replaceState(null, "", `/atelier/catalog/${encodeURIComponent(workflowName)}`);
  render(App, {
    props: {
      cockpitApi,
      mutationJournal: new MutationJournal(sessionStorage),
      createRunId: () => "run-order"
    }
  });
  await fireEvent.click(await screen.findByRole("button", { name: "Start" }));
  await screen.findByRole("dialog", { name: `Start ${workflowName}` });
  await waitFor(() => expect(screen.queryByText("Preparing…")).toBeNull());
}

beforeEach(() => sessionStorage.clear());

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
});

describe("the schema-generated fields on the catalog start sheet", () => {
  it("shows every declared order's published summary and no order form when the revision declares none", async () => {
    const cockpitApi = api();
    await openStart(cockpitApi);

    const order = await screen.findByRole("group", { name: "Order portions" });
    expect(order.textContent).toContain("portions-schema@schema-portions");
    expect(within(order).getByLabelText("portions (integer) *")).toBeTruthy();

    cleanup();
    const orderlessApi = api({ getWorkflowRevision: vi.fn(async () => detail([])) });
    await openStart(orderlessApi);
    expect(screen.queryByRole("group", { name: /^Order / })).toBeNull();
  });

  it("keeps Start run unavailable until every required schema field and role is chosen", async () => {
    const cockpitApi = api();
    await openStart(cockpitApi);

    const start = screen.getByRole("button", { name: "Start run" });
    expect((start as HTMLButtonElement).disabled).toBe(true);
    await fireEvent.change(screen.getByLabelText("cook"), { target: { value: configurationHash } });
    expect((start as HTMLButtonElement).disabled).toBe(true);
    await fireEvent.input(screen.getByLabelText("portions (integer) *"), { target: { value: "7" } });
    expect((start as HTMLButtonElement).disabled).toBe(false);
  });

  it("starts the selected revision with its typed schema values as named orders", async () => {
    const cockpitApi = api();
    await openStart(cockpitApi);

    await fireEvent.input(screen.getByLabelText("portions (integer) *"), { target: { value: "7" } });
    await fireEvent.change(screen.getByLabelText("cook"), { target: { value: configurationHash } });
    await fireEvent.click(screen.getByRole("button", { name: "Start run" }));

    await waitFor(() => expect(cockpitApi.start).toHaveBeenCalledTimes(1));
    const mutation = vi.mocked(cockpitApi.start).mock.calls[0]?.[0];
    const request = JSON.parse(globalThis.atob(mutation?.body_base64 ?? ""));
    expect(request.orders).toEqual([{ name: "portions", value: '{"portions":7}' }]);
    expect(request.agent_bindings).toEqual([
      { role: "cook", agent_configuration_revision_hash: configurationHash }
    ]);
    await waitFor(() =>
      expect(window.location.pathname).toBe(`/atelier/runs/${publicReference}`)
    );
  });

  it("groups observed work items by source and starts the selected item through its V3 wire shape", async () => {
    const cockpitApi = api({
      getWorkflowRevision: vi.fn(async () => detail([workItemOrder])),
      getSchemaRevision: vi.fn(async () => workItemSchema),
      listObservedQueueItems: vi.fn(async () => ({
        items: [
          { project_id: "atelier", tracker_item_reference: "gh:450", item_id: "1".repeat(64), revision: 0 },
          { project_id: "infra", tracker_item_reference: "gl:12", item_id: "2".repeat(64), revision: 0 }
        ],
        next_after: null
      }))
    });
    await openStart(cockpitApi);

    const picker = screen.getByLabelText("Work item for work");
    const workItemOrderGroup = screen.getByRole("group", { name: "Work item" });
    expect(workItemOrderGroup.textContent).not.toContain(`work-item-schema@${WORK_ITEM_ORDER_SCHEMA_REVISION}`);
    expect([...picker.querySelectorAll("optgroup")].map((group) => group.label)).toEqual([
      "GitHub",
      "GitLab"
    ]);
    expect(picker.querySelector('option[value="gh:450"]')?.textContent).toBe("GitHub · gh:450");
    expect(screen.queryByRole("note")).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Interim configuration" }));
    expect(screen.getByRole("status").textContent).toContain("Settings › Model defaults exist");
    expect(screen.getByText("Interim source · choose for this run")).toBeTruthy();

    await fireEvent.change(picker, { target: { value: "gh:450" } });
    expect((picker as HTMLSelectElement).selectedOptions[0]?.textContent).toBe("GitHub · gh:450");
    await fireEvent.change(screen.getByLabelText("Configuration for cook"), {
      target: { value: configurationHash }
    });
    expect((screen.getByLabelText("Configuration for cook") as HTMLSelectElement).value).toBe(
      configurationHash
    );
    await waitFor(() =>
      expect(screen.getByText("Interim source · chosen for this run, not saved")).toBeTruthy()
    );
    await fireEvent.click(screen.getByRole("button", { name: "Start run" }));

    await waitFor(() => expect(cockpitApi.start).toHaveBeenCalledTimes(1));
    const mutation = vi.mocked(cockpitApi.start).mock.calls[0]?.[0];
    const request = JSON.parse(globalThis.atob(mutation?.body_base64 ?? ""));
    expect(request.orders).toEqual([{ name: "work", work_item: "gh:450" }]);
  });

  it("holds Start when a work-item order has no observed source and leads to Settings", async () => {
    const cockpitApi = api({
      getWorkflowRevision: vi.fn(async () => detail([workItemOrder])),
      getSchemaRevision: vi.fn(async () => workItemSchema)
    });
    await openStart(cockpitApi);

    expect(screen.getByText("No source")).toBeTruthy();
    await fireEvent.change(screen.getByLabelText("Configuration for cook"), {
      target: { value: configurationHash }
    });
    expect((screen.getByRole("button", { name: "Start run" }) as HTMLButtonElement).disabled).toBe(true);
    await fireEvent.click(screen.getByRole("button", { name: "Settings" }));
    await waitFor(() => expect(window.location.pathname).toBe("/atelier/settings"));
  });

  it("refuses a work-item lookalike before it can enable Start", async () => {
    const cockpitApi = api({
      getWorkflowRevision: vi.fn(async () =>
        detail([{ ...workItemOrder, schema: { ...workItemOrder.schema, revision: "f".repeat(64) } }])
      ),
      getSchemaRevision: vi.fn(async () => workItemSchema)
    });
    await openStart(cockpitApi);

    expect((await screen.findByRole("alert")).textContent).toContain("canonical work-item schema");
    await fireEvent.change(screen.getByLabelText("Configuration for cook"), {
      target: { value: configurationHash }
    });
    expect((screen.getByRole("button", { name: "Start run" }) as HTMLButtonElement).disabled).toBe(true);
    expect(cockpitApi.start).not.toHaveBeenCalled();
  });

  it("refuses a scalar schema before it can send an invented object", async () => {
    const cockpitApi = api({
      getWorkflowRevision: vi.fn(async () =>
        detail([{ name: "approved", schema: { ref: "flag", revision: "schema-flag" } }])
      ),
      getSchemaRevision: vi.fn(async () => ({ type: "boolean" }))
    });
    await openStart(cockpitApi);

    expect((await screen.findByRole("alert")).textContent).toContain("must be an object");
    await fireEvent.change(screen.getByLabelText("Configuration for cook"), {
      target: { value: configurationHash }
    });
    expect((screen.getByRole("button", { name: "Start run" }) as HTMLButtonElement).disabled).toBe(true);
    expect(cockpitApi.start).not.toHaveBeenCalled();
  });

  it("opens modally, contains keyboard focus, and returns it to Start on Escape", async () => {
    const showModal = vi.fn(function (this: HTMLDialogElement) {
      this.open = true;
    });
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value: showModal
    });
    const cockpitApi = api();
    window.history.replaceState(null, "", `/atelier/catalog/${encodeURIComponent(workflowName)}`);
    render(App, {
      props: {
        cockpitApi,
        mutationJournal: new MutationJournal(sessionStorage),
        createRunId: () => "run-order"
      }
    });
    const start = await screen.findByRole("button", { name: "Start" });
    start.focus();
    await fireEvent.click(start);
    const dialog = await screen.findByRole("dialog", { name: `Start ${workflowName}` });
    await waitFor(() => expect(screen.queryByText("Preparing…")).toBeNull());
    const cancel = within(dialog).getByRole("button", { name: "Cancel" });
    const portions = within(dialog).getByLabelText("portions (integer) *");
    expect(showModal).toHaveBeenCalledOnce();
    expect(document.activeElement).toBe(cancel);
    expect(within(dialog).getAllByRole("button", { name: "Cancel" })).toHaveLength(1);

    await fireEvent.keyDown(cancel, { key: "Tab" });
    expect(document.activeElement).toBe(portions);
    await fireEvent.keyDown(portions, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(cancel);
    await fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: `Start ${workflowName}` })).toBeNull();
    await waitFor(() => expect(document.activeElement).toBe(start));
  });
});
