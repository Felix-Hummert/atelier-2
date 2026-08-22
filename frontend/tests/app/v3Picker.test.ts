import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import type { CockpitApi, RunV3 } from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { cockpitApiStub } from "../support/cockpitApi";

const revisionHash = "a".repeat(64);
const authHash = "b".repeat(64);
const configurationHash = "c".repeat(64);
const publicReference = "run1.cnVuLXYz";

function v3Revision(hash: string, documentBase64: string) {
  return {
    workflow_revision_hash: hash,
    document_base64: documentBase64,
    graph: {
      workflow_format_version: 3 as const,
      executable: true as const,
      not_executable_reason: null,
      node_count: 2,
      agent_roles: ["builder"],
      orders: [],
      node_previews: [
        {
          id: "implement",
          kind: "agent" as const,
          role: "builder",
          instruction_start: "Do the one thing this chain is for.",
          depends_on: []
        },
        {
          id: "review",
          kind: "agent" as const,
          role: "builder",
          instruction_start: "Check what the node before you did.",
          depends_on: ["implement"]
        }
      ],
      loops: [],
      name: "Seen from the picker",
      description: null
    }
  };
}

function startedV3Run(): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "run-v3",
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    agent_binding_set_hash: "d".repeat(64),
    run_configuration_revision_hash: "e".repeat(64),
    agent_bindings: [
      {
        role: "builder",
        agent_configuration_revision_hash: configurationHash,
        auth_profile_revision_hash: authHash,
        profile_id: "picker-v3",
        revision_number: 1,
        provider_id: "e2e-v3",
        auth_mode: "subscription",
        model: "v3-model",
        executor_revision: "immediate/v1"
      }
    ],
    state_version: 1,
    state: "STARTED",
    current_node_id: "implement",
    node_rail: [
      { node_id: "implement", state: "working", attempt: null },
      { node_id: "review", state: "queued", attempt: null }
    ],
    terminal_hash: null,
    latest_event_cursor: null
  };
}

beforeEach(() => {
  sessionStorage.clear();
  window.history.replaceState(null, "", "/atelier/new");
});

afterEach(() => cleanup());

async function publishAndBind(cockpitApi: CockpitApi): Promise<void> {
  render(App, {
    props: {
      cockpitApi,
      mutationJournal: new MutationJournal(sessionStorage),
      createRunId: () => "run-v3"
    }
  });
  await fireEvent.click(await screen.findByLabelText("Publish YAML"));
  await fireEvent.input(screen.getByLabelText("Exact workflow YAML"), {
    target: { value: "format_version: 3\nname: Seen from the picker\n" }
  });
  await fireEvent.click(screen.getByRole("button", { name: "Review publication" }));
  const dialog = screen.getByRole("dialog", { name: "Publish this exact workflow?" });
  await fireEvent.click(within(dialog).getByRole("button", { name: "Publish" }));
  await waitFor(() => expect(cockpitApi.publish).toHaveBeenCalledTimes(1));
}

function api(overrides: Partial<CockpitApi> = {}): CockpitApi {
  return cockpitApiStub({
    publish: vi.fn(async (mutation) =>
      ({
        status: 201,
        value: v3Revision(mutation.mutation_id.slice("publish:".length), mutation.body_base64)
      }) as never
    ),
    publishAuthProfile: vi.fn(async (input) =>
      ({ status: 201, value: { ...input, auth_profile_revision_hash: authHash } }) as never
    ),
    publishAgentConfiguration: vi.fn(async (input) =>
      ({
        status: 201,
        value: {
          ...input,
          provider_id: "e2e-v3",
          auth_mode: "subscription" as const,
          requested_capability: input.requested_capability ?? ("headless" as const),
          agent_configuration_revision_hash: configurationHash
        }
      }) as never
    ),
    start: vi.fn(async () => ({ status: 201, value: startedV3Run() }) as never),
    getRun: vi.fn(async () => startedV3Run()),
    ...overrides
  });
}

describe("starting a version 3 workflow from the picker", () => {
  it("proves(a-v3-workflow-is-started-from-the-picker): offers a binding card for each role the API named", async () => {
    // The roles used to be read out of the graph's nodes, which a version 3
    // revision does not put on the wire, so the picker offered no binding at all
    // and jumped straight to Start. The API names them now, and the picker asks
    // for exactly those.
    const cockpitApi = api();

    await publishAndBind(cockpitApi);

    const binding = await screen.findByRole("article", { name: "Binding builder" });
    expect(binding.isConnected).toBe(true);
    expect(screen.getAllByRole("article", { name: /^Binding / })).toHaveLength(1);
  });

  it("proves(a-cockpit-published-v3-workflow-is-named-over-the-api): names a legal published title after publish", async () => {
    const legal = v3Revision(revisionHash, "YQ==");
    legal.graph = { ...legal.graph, name: "diff-review" };
    const cockpitApi = api({
      publish: vi.fn(async () => ({ status: 201, value: legal })),
      foundCatalogLineage: vi.fn(async () => ({
        status: 201,
        value: {
          display_name: "diff-review",
          lineage_id: "f".repeat(64),
          workflow_revision_hash: revisionHash,
          revision_number: 1
        }
      }))
    });

    render(App, {
      props: {
        cockpitApi,
        mutationJournal: new MutationJournal(sessionStorage),
        createRunId: () => "run-v3"
      }
    });
    await fireEvent.click(await screen.findByLabelText("Publish YAML"));
    await fireEvent.input(screen.getByLabelText("Exact workflow YAML"), {
      target: { value: "format_version: 3\nname: diff-review\n" }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Review publication" }));
    const dialog = screen.getByRole("dialog", { name: "Publish this exact workflow?" });
    await fireEvent.click(within(dialog).getByRole("button", { name: "Publish" }));

    await waitFor(() => expect(cockpitApi.foundCatalogLineage).toHaveBeenCalledTimes(1));
    expect(vi.mocked(cockpitApi.foundCatalogLineage).mock.calls[0]?.[0]).toEqual({
      workflow_revision_hash: revisionHash,
      actor: "atelier2-cockpit",
      activated_at: expect.stringMatching(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/)
    });
  });

  it("proves(a-v3-workflow-is-started-from-the-picker): starts it through the bound request its bindings describe", async () => {
    const cockpitApi = api();
    await publishAndBind(cockpitApi);

    await fireEvent.input(screen.getByLabelText("Profile ID"), { target: { value: "picker-v3" } });
    await fireEvent.input(screen.getByLabelText("Revision"), { target: { value: "1" } });
    await fireEvent.input(screen.getByLabelText("Provider"), { target: { value: "e2e-v3" } });
    await fireEvent.change(screen.getByLabelText("Auth mode"), { target: { value: "subscription" } });
    await fireEvent.input(screen.getByLabelText("Model"), { target: { value: "v3-model" } });
    await fireEvent.input(screen.getByLabelText("Executor"), { target: { value: "immediate/v1" } });
    await fireEvent.click(screen.getByRole("button", { name: "Start" }));

    await waitFor(() => expect(cockpitApi.start).toHaveBeenCalledTimes(1));
    const mutation = vi.mocked(cockpitApi.start).mock.calls[0]?.[0];
    const body = JSON.parse(globalThis.atob(mutation?.body_base64 ?? ""));
    expect(body.workflow_revision_hash).toMatch(/^[0-9a-f]{64}$/);
    expect(body.agent_bindings).toEqual([
      { role: "builder", agent_configuration_revision_hash: configurationHash }
    ]);
  });
});
