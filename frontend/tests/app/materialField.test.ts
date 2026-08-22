import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import { CockpitRequestError, type CockpitApi, type RunV3 } from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { cockpitApiStub } from "../support/cockpitApi";
import { utf8Base64 } from "../support/exactBytes";

const revisionHash = "a".repeat(64);
const otherHash = "b".repeat(64);
const authHash = "c".repeat(64);
const configurationHash = "d".repeat(64);
const publicReference = "run1.cnVuLW9yZGVy";

const portionsOrder = {
  name: "portions",
  schema: {
    ref: "portions-schema",
    revision: "schema-portions"
  }
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
    state_version: 1,
    state: "STARTED",
    current_node_id: "cook",
    node_rail: [{ node_id: "cook", state: "working", attempt: null }],
    terminal_hash: null,
    latest_event_cursor: null
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
    listAgentConfigurationRevisions: vi.fn(async () => ({
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
    })),
    start: vi.fn(async () => ({ status: 201, value: startedRun() })),
    getRun: vi.fn(async () => startedRun()),
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

beforeEach(() => {
  sessionStorage.clear();
  window.history.replaceState(null, "", "/atelier/new");
});

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
});

describe("the material field on start", () => {
  it("shows one field per declared order and none when the revision declares none", async () => {
    const cockpitApi = api();
    await openStart(cockpitApi);

    await fireEvent.click(await screen.findByRole("radio", { name: /Cook to order/ }));
    const field = await screen.findByRole("article", { name: "Order portions" });
    expect(field.textContent).toContain("portions-schema@schema-portions");
    expect(within(field).getByLabelText("Material portions")).toBeTruthy();
    expect(within(field).queryByPlaceholderText(/.+/)).toBeNull();
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

    await fireEvent.click(await screen.findByRole("radio", { name: /Cook to order/ }));
    await screen.findByRole("article", { name: "Order portions" });
    await fireEvent.change(screen.getByLabelText("Agent for cook"), {
      target: { value: configurationHash }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Start" }));

    expect((await screen.findByRole("alert")).textContent).toContain(
      "input 'portions' was refused: missing"
    );
    expect(cockpitApi.start).not.toHaveBeenCalled();
  });

  it("sends the typed material as the named order on the V3 start", async () => {
    const cockpitApi = api();
    await openStart(cockpitApi);

    await fireEvent.click(await screen.findByRole("radio", { name: /Cook to order/ }));
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

  it("shows the server's own refusal words, not a generic start failure", async () => {
    const cockpitApi = api({
      start: vi.fn(async () => {
        throw new CockpitRequestError(
          "input 'portions' was refused: value-refused",
          {
            type: "urn:atelier2:problem:v1:run-input-refused",
            title: "Run input refused",
            status: 422,
            detail: "input 'portions' was refused: value-refused"
          },
          true
        );
      })
    });
    await openStart(cockpitApi);

    await fireEvent.click(await screen.findByRole("radio", { name: /Cook to order/ }));
    await fireEvent.input(await screen.findByLabelText("Material portions"), {
      target: { value: "not-json" }
    });
    await fireEvent.change(screen.getByLabelText("Agent for cook"), {
      target: { value: configurationHash }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Start" }));

    const notice = await screen.findByRole("alert");
    expect(notice.textContent).toContain("input 'portions' was refused: value-refused");
    expect(notice.textContent).not.toContain("The run start could not be confirmed.");
  });
});
