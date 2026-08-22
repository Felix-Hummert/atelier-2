import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  agentConfigurationRevisionPageSchema,
  type AgentConfigurationRevisionListItem,
  type CockpitApi,
  type RunV3
} from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import {
  NAMED_AGENT_CHOICE_STORAGE_KEY,
  namedAgentLabel
} from "../../src/lib/namedAgentChoice";
import { cockpitApiStub } from "../support/cockpitApi";

const revisionHash = "a".repeat(64);
const authHash = "b".repeat(64);
const configurationHash = "c".repeat(64);
const publicReference = "run1.cnVuLW5hbWVk";

const servedDocument = JSON.parse(
  readFileSync(resolve(process.cwd(), "..", "tests", "api", "openapi_frozen.json"), "utf8")
) as { components: { schemas: Record<string, { properties?: Record<string, unknown> }> } };

function publishedAgent(
  changes: Partial<AgentConfigurationRevisionListItem> = {}
): AgentConfigurationRevisionListItem {
  const publication = {
    model: "sonnet",
    auth_profile_revision_hash: authHash,
    executor_revision: "claude-subscription/v1",
    provider_id: "anthropic",
    auth_mode: "subscription" as const,
    requested_capability: "headless" as const,
    agent_configuration_revision_hash: configurationHash
  };
  const served = servedDocument.components.schemas.AgentConfigurationRevisionResource;
  expect(Object.keys(publication).sort()).toEqual(Object.keys(served?.properties ?? {}).sort());
  return {
    ...publication,
    startable: true,
    not_startable_reason: null,
    ...changes
  };
}

function v3Revision(hash: string, documentBase64: string) {
  return {
    workflow_revision_hash: hash,
    document_base64: documentBase64,
    graph: {
      workflow_format_version: 3 as const,
      executable: true as const,
      not_executable_reason: null,
      node_count: 1,
      agent_roles: ["builder"],
      orders: [],
      node_previews: [
        {
          id: "implement",
          kind: "agent" as const,
          role: "builder",
          instruction_start: "Do the one thing.",
          depends_on: []
        }
      ],
      loops: [],
      name: "Named start",
      description: null
    }
  };
}

function startedV3Run(): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "run-named",
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    agent_binding_set_hash: "d".repeat(64),
    run_configuration_revision_hash: "e".repeat(64),
    agent_bindings: [
      {
        role: "builder",
        agent_configuration_revision_hash: configurationHash,
        auth_profile_revision_hash: authHash,
        profile_id: "max",
        revision_number: 1,
        provider_id: "anthropic",
        auth_mode: "subscription",
        model: "sonnet",
        executor_revision: "claude-subscription/v1"
      }
    ],
    state_version: 1,
    state: "STARTED",
    current_node_id: "implement",
    node_rail: [{ node_id: "implement", state: "working", attempt: null }],
    terminal_hash: null,
    latest_event_cursor: null
  };
}

function api(overrides: Partial<CockpitApi> = {}): CockpitApi {
  return cockpitApiStub({
    publish: vi.fn(async (mutation) =>
      ({
        status: 201,
        value: v3Revision(mutation.mutation_id.slice("publish:".length), mutation.body_base64)
      }) as never
    ),
    start: vi.fn(async () => ({ status: 201, value: startedV3Run() }) as never),
    getRun: vi.fn(async () => startedV3Run()),
    ...overrides
  });
}

async function publishWorkflow(cockpitApi: CockpitApi): Promise<void> {
  render(App, {
    props: {
      cockpitApi,
      mutationJournal: new MutationJournal(sessionStorage),
      createRunId: () => "run-named"
    }
  });
  await fireEvent.click(await screen.findByLabelText("Publish YAML"));
  await fireEvent.input(screen.getByLabelText("Exact workflow YAML"), {
    target: { value: "format_version: 3\nname: Named start\n" }
  });
  await fireEvent.click(screen.getByRole("button", { name: "Review publication" }));
  const dialog = screen.getByRole("dialog", { name: "Publish this exact workflow?" });
  await fireEvent.click(within(dialog).getByRole("button", { name: "Publish" }));
  await waitFor(() => expect(cockpitApi.publish).toHaveBeenCalledTimes(1));
}

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  window.history.replaceState(null, "", "/atelier/new");
});

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
});

describe("named agent picker", () => {
  it("names the empty list and the next step, never silently", async () => {
    const cockpitApi = api();
    await publishWorkflow(cockpitApi);

    const binding = await screen.findByRole("article", { name: "Binding builder" });
    expect(screen.getAllByText("No published agents yet.")).toHaveLength(1);
    expect(binding.textContent).toContain("Expert fields");
    expect(within(binding).queryByLabelText("Agent for builder")).toBeNull();
    expect(within(binding).getByText("Expert fields").closest("details")?.open).toBe(false);
  });

  it("confirms every agent page together before replacing an initial failure", async () => {
    const secondHash = "d".repeat(64);
    const first = publishedAgent();
    const second = publishedAgent({
      agent_configuration_revision_hash: secondHash,
      provider_id: "openai",
      model: "codex"
    });
    const listAgentConfigurationRevisions = vi
      .fn()
      .mockResolvedValueOnce({ items: [first], next_after_revision_hash: configurationHash })
      .mockResolvedValueOnce({ items: [first], next_after_revision_hash: configurationHash })
      .mockResolvedValueOnce({ items: [first], next_after_revision_hash: configurationHash })
      .mockResolvedValueOnce({ items: [second], next_after_revision_hash: null });
    const cockpitApi = api({ listAgentConfigurationRevisions });

    await publishWorkflow(cockpitApi);

    await screen.findByText("Published agents incomplete");
    expect(screen.queryByText("No published agents yet.")).toBeNull();
    expect(screen.queryByLabelText("Agent for builder")).toBeNull();
    expect(screen.queryByText(/cursor it had already given/i)).toBeNull();
    expect(screen.getAllByRole("button", { name: "Retry published agents" })).toHaveLength(1);

    await fireEvent.click(screen.getByRole("button", { name: "Retry published agents" }));

    const picker = await screen.findByLabelText("Agent for builder");
    expect(picker.textContent).toContain("anthropic · sonnet · Subscription");
    expect(picker.textContent).toContain("openai · codex · Subscription");
    expect(listAgentConfigurationRevisions.mock.calls).toEqual([
      [undefined],
      [configurationHash],
      [undefined],
      [configurationHash]
    ]);
  });

  it("keeps a manual choice and expert draft once the agent list is confirmed, offering no manual refresh", async () => {
    const chosenHash = "d".repeat(64);
    const first = publishedAgent();
    const chosen = publishedAgent({
      agent_configuration_revision_hash: chosenHash,
      provider_id: "openai",
      model: "codex"
    });
    const listAgentConfigurationRevisions = vi.fn(async () => ({
      items: [first, chosen],
      next_after_revision_hash: null
    }));
    const cockpitApi = api({ listAgentConfigurationRevisions });
    await publishWorkflow(cockpitApi);

    const binding = await screen.findByRole("article", { name: "Binding builder" });
    const picker = within(binding).getByLabelText("Agent for builder") as HTMLSelectElement;
    await fireEvent.change(picker, { target: { value: chosenHash } });
    await fireEvent.click(within(binding).getByText("Expert fields"));
    const expertValues = {
      "Profile ID": "manual-profile",
      Revision: "7",
      Provider: "manual-provider",
      Model: "manual-model",
      Executor: "manual/v1"
    } as const;
    for (const [label, value] of Object.entries(expertValues)) {
      await fireEvent.input(within(binding).getByLabelText(label), { target: { value } });
    }
    await fireEvent.change(within(binding).getByLabelText("Auth mode"), {
      target: { value: "api_key" }
    });

    expect(picker.value).toBe(chosenHash);
    for (const [label, value] of Object.entries(expertValues)) {
      expect(within(binding).getByLabelText(label)).toHaveProperty("value", value);
    }
    expect(within(binding).getByLabelText("Auth mode")).toHaveProperty("value", "api_key");
    expect(screen.queryByRole("button", { name: /published agents/ })).toBeNull();
  });

  it("offers a published agent as provider · model · readable auth, and starts with that hash", async () => {
    const agent = publishedAgent();
    const cockpitApi = api({
      listAgentConfigurationRevisions: vi.fn(async () => ({
        items: [agent],
        next_after_revision_hash: null
      }))
    });
    await publishWorkflow(cockpitApi);

    const binding = await screen.findByRole("article", { name: "Binding builder" });
    const picker = within(binding).getByLabelText("Agent for builder");
    expect(namedAgentLabel(agent)).toBe("anthropic · sonnet · Subscription");
    expect(picker.textContent).toContain("anthropic · sonnet · Subscription");
    expect(picker.textContent).not.toContain("subscription");
    await fireEvent.change(picker, { target: { value: configurationHash } });
    await fireEvent.click(screen.getByRole("button", { name: "Start" }));

    await waitFor(() => expect(cockpitApi.start).toHaveBeenCalledTimes(1));
    expect(cockpitApi.publishAuthProfile).not.toHaveBeenCalled();
    expect(cockpitApi.publishAgentConfiguration).not.toHaveBeenCalled();
    const body = JSON.parse(globalThis.atob(vi.mocked(cockpitApi.start).mock.calls[0]?.[0].body_base64 ?? ""));
    expect(body.agent_bindings).toEqual([
      { role: "builder", agent_configuration_revision_hash: configurationHash }
    ]);
    expect(localStorage.getItem(NAMED_AGENT_CHOICE_STORAGE_KEY)).toContain(configurationHash);
  });

  it("preselects the last choice for that role so the daily path is Start", async () => {
    localStorage.setItem(
      NAMED_AGENT_CHOICE_STORAGE_KEY,
      JSON.stringify({ builder: configurationHash })
    );
    const cockpitApi = api({
      listAgentConfigurationRevisions: vi.fn(async () => ({
        items: [publishedAgent()],
        next_after_revision_hash: null
      }))
    });
    await publishWorkflow(cockpitApi);

    const picker = await screen.findByLabelText("Agent for builder");
    expect((picker as HTMLSelectElement).value).toBe(configurationHash);
    await fireEvent.click(screen.getByRole("button", { name: "Start" }));

    await waitFor(() => expect(cockpitApi.start).toHaveBeenCalledTimes(1));
    expect(cockpitApi.publishAuthProfile).not.toHaveBeenCalled();
  });

  it("keeps a remembered unavailable choice visible and requires a healthy manual switch", async () => {
    const healthyHash = "d".repeat(64);
    localStorage.setItem(
      NAMED_AGENT_CHOICE_STORAGE_KEY,
      JSON.stringify({ builder: configurationHash })
    );
    const unavailable = publishedAgent({
      startable: false,
      not_startable_reason: "agent-executor-binding-unavailable"
    });
    const healthy = publishedAgent({
      agent_configuration_revision_hash: healthyHash,
      provider_id: "openai",
      model: "codex"
    });
    const cockpitApi = api({
      listAgentConfigurationRevisions: vi.fn(async () => ({
        items: [unavailable, healthy],
        next_after_revision_hash: null
      }))
    });

    await publishWorkflow(cockpitApi);

    const binding = await screen.findByRole("article", { name: "Binding builder" });
    const picker = within(binding).getByLabelText("Agent for builder") as HTMLSelectElement;
    expect(picker.value).toBe(configurationHash);
    expect(
      within(binding).getByLabelText("Binding source: Remembered").isConnected
    ).toBe(true);
    expect(within(binding).getByText("Unavailable").isConnected).toBe(true);
    expect(
      (within(binding).getByRole("option", { name: /sonnet.*Unavailable/ }) as HTMLOptionElement)
        .disabled
    ).toBe(true);
    expect(screen.queryByRole("button", { name: "Start" })).toBeNull();
    await fireEvent.click(within(binding).getByRole("button", { name: "Why builder is unavailable" }));
    expect(
      within(binding).getByText(
        "This deployment cannot start this executor. Choose another agent or repair its startup check."
      ).isConnected
    ).toBe(true);
    expect(cockpitApi.start).not.toHaveBeenCalled();

    await fireEvent.change(picker, { target: { value: healthyHash } });
    await fireEvent.click(screen.getByRole("button", { name: "Start" }));

    await waitFor(() => expect(cockpitApi.start).toHaveBeenCalledTimes(1));
    const body = JSON.parse(globalThis.atob(vi.mocked(cockpitApi.start).mock.calls[0]?.[0].body_base64 ?? ""));
    expect(body.agent_bindings).toEqual([
      { role: "builder", agent_configuration_revision_hash: healthyHash }
    ]);
  });

  it("decodes the published listing as the frozen page", () => {
    const served = servedDocument.components.schemas.AgentConfigurationRevisionPageResource;
    const page = {
      items: [publishedAgent()],
      next_after_revision_hash: null
    };
    expect(Object.keys(page).sort()).toEqual(Object.keys(served?.properties ?? {}).sort());
    expect(agentConfigurationRevisionPageSchema.parse(page)).toEqual(page);
  });
});
