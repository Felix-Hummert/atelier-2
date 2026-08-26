import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import { CockpitRequestError, decodeProblem, type CockpitApi, type RunV3 } from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { cockpitApiStub } from "../support/cockpitApi";
import { cancellableBlock } from "../support/runV3";
import { utf8Base64 } from "../support/exactBytes";

const revisionHash = "a".repeat(64);
const otherHash = "b".repeat(64);
const authHash = "c".repeat(64);
const configurationHash = "d".repeat(64);
const publicReference = "run1.cnVuLW9yZGVy";
const projectReference = "project1.dGVzdA";

const portionsOrder = {
  name: "portions",
  schema: {
    ref: "portions-schema",
    revision: "schema-portions"
  }
};

// A single required field, but not a bare string -- the JSON editor stays,
// exactly the shape most declared orders carry today.
const portionsSchema = {
  type: "object",
  required: ["portions"],
  additionalProperties: false,
  properties: { portions: { type: "integer", minimum: 0 } }
};

function summary(hash: string, name: string) {
  return {
    workflow_revision_hash: hash,
    workflow_format_version: 3 as const,
    executable: true,
    not_executable_reason: null,
    name,
    description: null
  };
}

function graph(orders: Array<typeof portionsOrder>, name: string) {
  return {
    workflow_format_version: 3 as const,
    executable: true as const,
    not_executable_reason: null,
    node_count: 1,
    agent_roles: ["cook"],
    orders,
    wait_answer_schemas: [],
    node_previews: [
      {
        id: "cook",
        kind: "agent" as const,
        role: "cook",
        instruction_start: "Cook exactly what the order says.",
        depends_on: []
      }
    ],
    loops: [],
    name,
    description: null
  };
}

function detail(hash: string, orders: Array<typeof portionsOrder>, name: string) {
  return {
    workflow_revision_hash: hash,
    document_base64: utf8Base64("job: NEVER_PARSE_THIS\n"),
    graph: graph(orders, name)
  };
}

function startedRun(): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "run-order",
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    agent_binding_set_hash: "e".repeat(64),
    run_configuration_revision_hash: "f".repeat(64),
    agent_bindings: [
      {
        role: "cook",
        agent_configuration_revision_hash: configurationHash,
        auth_profile_revision_hash: authHash,
        profile_id: "max",
        revision_number: 1,
        provider_id: "exact",
        auth_mode: "subscription",
        model: "cook-model",
        executor_revision: "immediate/v1"
      }
    ],
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

function publishedCookConfigurations() {
  return {
    items: [
      {
        model: "cook-model",
        auth_profile_revision_hash: authHash,
        executor_revision: "immediate/v1",
        provider_id: "exact",
        auth_mode: "subscription" as const,
        requested_capability: "headless" as const,
        agent_configuration_revision_hash: configurationHash,
        startable: true,
        not_startable_reason: null
      }
    ],
    next_after_revision_hash: null
  };
}

function projectModelApi(): Pick<CockpitApi, "listProjects" | "resolveProjectModels"> {
  return {
    listProjects: vi.fn(async () => ({ items: [{ public_project_reference: projectReference }] })),
    resolveProjectModels: vi.fn(async (
      _project: string,
      workflowHash: string,
      modelOverrides: Parameters<CockpitApi["resolveProjectModels"]>[2]
    ) => {
      const chosen = modelOverrides.find((item) => item.role === "cook")
        ?.agent_configuration_revision_hash ?? configurationHash;
      return {
        project_id: "atelier",
        public_project_reference: projectReference,
        workflow_revision_hash: workflowHash,
        resolutions: [{
          role: "cook",
          agent_configuration_revision_hash: chosen,
          source: modelOverrides.length === 0 ? "from-project" as const : "chosen-now" as const,
          model_id: "cook-model",
          declared_difficulty: 2 as const,
          default_difficulty: modelOverrides.length === 0 ? 2 as const : null,
          uncast_reason: null,
          family_differs_from: null
        }]
      };
    })
  };
}

function api(overrides: Partial<CockpitApi> = {}): CockpitApi {
  return cockpitApiStub({
    listWorkflowRevisions: vi.fn(async () => ({
      items: [
        summary(revisionHash, "Cook to order"),
        summary(otherHash, "One agent")
      ],
      next_after_revision_hash: null
    })),
    getWorkflowRevision: vi.fn(async (hash: string) =>
      hash === otherHash
        ? detail(otherHash, [], "One agent")
        : detail(revisionHash, [portionsOrder], "Cook to order")
    ),
    getSchemaRevision: vi.fn(async () => portionsSchema),
    listAgentConfigurationRevisions: vi.fn(async () => publishedCookConfigurations()),
    start: vi.fn(async () => ({ status: 201, value: startedRun() })),
    getRun: vi.fn(async () => startedRun()),
    ...projectModelApi(),
    ...overrides
  });
}

async function openStart(cockpitApi: CockpitApi): Promise<void> {
  render(App, {
    props: {
      cockpitApi,
      mutationJournal: new MutationJournal(sessionStorage),
      createRunId: () => "run-order"
    }
  });
}

async function chooseCookToOrder(): Promise<void> {
  await fireEvent.click(await screen.findByRole("radio", { name: /Cook to order/ }));
  await screen.findByRole("article", { name: "Order portions" });
}

beforeEach(() => {
  sessionStorage.clear();
  window.history.replaceState(null, "", "/atelier/new");
});

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
});

describe("the material field on start", () => {
  it("shows one field per declared order, its published schema summary, and none when the revision declares none", async () => {
    const cockpitApi = api();
    await openStart(cockpitApi);

    await chooseCookToOrder();
    const field = await screen.findByRole("article", { name: "Order portions" });
    expect(field.textContent).toContain("portions-schema@schema-portions");
    expect(within(field).getByLabelText("Material portions")).toBeTruthy();
    expect(within(field).queryByPlaceholderText(/.+/)).toBeNull();
    const fields = await within(field).findByRole("region", { name: "Fields of portions" });
    expect(fields.textContent).toContain("portions");
    expect(fields.textContent).toContain("integer");
    expect(fields.textContent).toContain("Required");
    expect(screen.queryByText("Issue")).toBeNull();
    expect(screen.queryByText("URL")).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Change" }));
    await fireEvent.click(await screen.findByRole("radio", { name: /One agent/ }));
    await waitFor(() => {
      expect(screen.queryByRole("article", { name: /^Order / })).toBeNull();
    });
    expect(screen.queryByLabelText(/^Material /)).toBeNull();
    expect(screen.queryByText(/no material/i)).toBeNull();
    expect(vi.mocked(cockpitApi.getWorkflowRevision).mock.calls.map(([hash]) => hash)).toEqual([
      revisionHash,
      otherHash
    ]);
  });

  it("refuses a missing order by name before anything is started", async () => {
    const cockpitApi = api();
    await openStart(cockpitApi);

    await chooseCookToOrder();
    await fireEvent.change(screen.getByLabelText("Agent for cook"), {
      target: { value: configurationHash }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Start" }));

    expect((await screen.findByRole("alert")).textContent).toContain(
      "input 'portions' was refused: missing"
    );
    expect(cockpitApi.start).not.toHaveBeenCalled();
  });

  it("refuses locally a value that plainly disagrees with the published schema, before any round trip", async () => {
    const cockpitApi = api();
    await openStart(cockpitApi);

    await chooseCookToOrder();
    const material = await screen.findByLabelText("Material portions");
    await fireEvent.input(material, { target: { value: "not-json" } });
    await fireEvent.change(screen.getByLabelText("Agent for cook"), {
      target: { value: configurationHash }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Start" }));

    expect((await screen.findByRole("alert")).textContent).toContain("This is not valid JSON.");
    expect(cockpitApi.start).not.toHaveBeenCalled();
  });

  it("sends the typed material as the named order on the V3 start", async () => {
    const cockpitApi = api();
    await openStart(cockpitApi);

    await chooseCookToOrder();
    const material = await screen.findByLabelText("Material portions");
    await fireEvent.input(material, { target: { value: '{"portions": 7}' } });
    await fireEvent.change(screen.getByLabelText("Agent for cook"), {
      target: { value: configurationHash }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Start" }));

    await waitFor(() => expect(cockpitApi.start).toHaveBeenCalledTimes(1));
    const body = JSON.parse(
      globalThis.atob(vi.mocked(cockpitApi.start).mock.calls[0]?.[0].body_base64 ?? "")
    );
    expect(body.workflow_format_version).toBe(3);
    expect(body.orders).toEqual([{ name: "portions", value: '{"portions": 7}' }]);
    expect(body.agent_bindings).toEqual([
      { role: "cook", agent_configuration_revision_hash: configurationHash }
    ]);
  });

  it("shows the server's own refusal words for a violation the browser's shallow check cannot see, with the field beside the order", async () => {
    const cockpitApi = api({
      start: vi.fn(async () => {
        throw new CockpitRequestError(
          "input 'portions' was refused: value-refused: /portions: -1 is less than the minimum of 0",
          decodeProblem({
            type: "urn:atelier2:problem:v1:run-input-refused",
            title: "Run input refused",
            status: 422,
            detail: "input 'portions' was refused: value-refused: /portions: -1 is less than the minimum of 0",
            invalid_fields: [{ path: "/portions", reason: "-1 is less than the minimum of 0" }]
          }),
          true
        );
      })
    });
    await openStart(cockpitApi);

    await chooseCookToOrder();
    // Shape-valid and type-correct, so the browser's own shallow check admits
    // it; only the server's schema evaluation (`minimum: 0`) refuses it.
    await fireEvent.input(await screen.findByLabelText("Material portions"), {
      target: { value: '{"portions": -1}' }
    });
    await fireEvent.change(screen.getByLabelText("Agent for cook"), {
      target: { value: configurationHash }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Start" }));

    const field = await screen.findByRole("article", { name: "Order portions" });
    await within(field).findByText(/is less than the minimum of 0/);
    const fieldAlerts = within(field).getAllByRole("alert").map((node) => node.textContent);
    expect(fieldAlerts.some((text) => text?.includes("/portions"))).toBe(true);
    expect(fieldAlerts.some((text) => text?.includes("is less than the minimum of 0"))).toBe(true);
    const topBanner = screen.getAllByRole("alert").find((node) => !field.contains(node));
    expect(topBanner?.textContent).toContain("input 'portions' was refused: value-refused");
  });
});

describe("a human editor for an order whose schema names exactly one required string field", () => {
  const noteOrder = {
    name: "note",
    schema: { ref: "note-schema", revision: "schema-note" }
  };
  const noteSchema = {
    type: "object",
    required: ["message"],
    properties: { message: { type: "string", minLength: 1 } }
  };

  function noteApi(overrides: Partial<CockpitApi> = {}): CockpitApi {
    return cockpitApiStub({
      listWorkflowRevisions: vi.fn(async () => ({
        items: [summary(revisionHash, "Leave a note")],
        next_after_revision_hash: null
      })),
      getWorkflowRevision: vi.fn(async () => detail(revisionHash, [noteOrder], "Leave a note")),
      getSchemaRevision: vi.fn(async () => noteSchema),
      listAgentConfigurationRevisions: vi.fn(async () => publishedCookConfigurations()),
      start: vi.fn(async () => ({ status: 201, value: startedRun() })),
      getRun: vi.fn(async () => startedRun()),
      ...projectModelApi(),
      ...overrides
    });
  }

  it("offers plain text and wraps it into the schema's own field on start", async () => {
    const cockpitApi = noteApi();
    await openStart(cockpitApi);

    await fireEvent.click(await screen.findByRole("radio", { name: /Leave a note/ }));
    const material = await screen.findByLabelText("Material note");
    expect(material.tagName).toBe("INPUT");
    await fireEvent.input(material, { target: { value: "remember the deploy window" } });
    await fireEvent.change(screen.getByLabelText("Agent for cook"), {
      target: { value: configurationHash }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Start" }));

    await waitFor(() => expect(cockpitApi.start).toHaveBeenCalledTimes(1));
    const body = JSON.parse(
      globalThis.atob(vi.mocked(cockpitApi.start).mock.calls[0]?.[0].body_base64 ?? "")
    );
    expect(body.orders).toEqual([
      { name: "note", value: '{"message":"remember the deploy window"}' }
    ]);
  });
});

describe("a multi-field order that stays JSON", () => {
  const briefOrder = {
    name: "brief",
    schema: { ref: "conductor-brief", revision: "schema-brief" }
  };
  const briefSchema = {
    type: "object",
    required: ["message", "prior_transcript", "dropped_oldest_messages"],
    additionalProperties: false,
    properties: {
      message: { type: "string", minLength: 1 },
      prior_transcript: { type: "array" },
      dropped_oldest_messages: { type: "integer", minimum: 0 }
    }
  };

  function briefApi(): CockpitApi {
    return cockpitApiStub({
      listWorkflowRevisions: vi.fn(async () => ({
        items: [summary(revisionHash, "Chat with conductor")],
        next_after_revision_hash: null
      })),
      getWorkflowRevision: vi.fn(async () => detail(revisionHash, [briefOrder], "Chat with conductor")),
      getSchemaRevision: vi.fn(async () => briefSchema),
      listAgentConfigurationRevisions: vi.fn(async () => publishedCookConfigurations()),
      ...projectModelApi(),
      start: vi.fn(),
      getRun: vi.fn()
    });
  }

  it("keeps the JSON editor and names every missing field honestly, without inventing a default", async () => {
    const cockpitApi = briefApi();
    await openStart(cockpitApi);

    await fireEvent.click(await screen.findByRole("radio", { name: /Chat with conductor/ }));
    const field = await screen.findByRole("article", { name: "Order brief" });
    expect(within(field).getByLabelText("Material brief").tagName).toBe("TEXTAREA");
    expect(field.textContent).toContain("more than one field");

    await fireEvent.input(within(field).getByLabelText("Material brief"), {
      target: { value: '{"message": "help"}' }
    });
    await fireEvent.change(screen.getByLabelText("Agent for cook"), {
      target: { value: configurationHash }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Start" }));

    const alerts = within(await screen.findByRole("article", { name: "Order brief" }))
      .getAllByRole("alert")
      .map((node) => node.textContent ?? "");
    expect(alerts.some((text) => text.includes("/prior_transcript"))).toBe(true);
    expect(alerts.some((text) => text.includes("/dropped_oldest_messages"))).toBe(true);
    expect(cockpitApi.start).not.toHaveBeenCalled();
  });
});
