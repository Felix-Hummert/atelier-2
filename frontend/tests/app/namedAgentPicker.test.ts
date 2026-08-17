import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  agentConfigurationRevisionPageSchema,
  type AgentConfigurationRevision,
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

function publishedAgent(changes: Partial<AgentConfigurationRevision> = {}): AgentConfigurationRevision {
  const item = {
    model: "sonnet",
    auth_profile_revision_hash: authHash,
    executor_revision: "claude-subscription/v1",
    provider_id: "anthropic",
    auth_mode: "subscription" as const,
    requested_capability: "headless" as const,
    agent_configuration_revision_hash: configurationHash,
    ...changes
  };
  const served = servedDocument.components.schemas.AgentConfigurationRevisionResource;
  expect(Object.keys(item).sort()).toEqual(Object.keys(served?.properties ?? {}).sort());
  return item;
}

function v3Revision(hash: string, documentBase64: string) {
  return {
    revision_hash: hash,
    document_base64: documentBase64,
    graph: {
      format_version: 3 as const,
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
    expect(binding.textContent).toContain("No published agent configurations yet.");
    expect(binding.textContent).toContain("Publish one below, then it will appear here.");
    expect(binding.textContent).toContain("Expert fields");
    expect(within(binding).queryByLabelText("Agent for builder")).toBeNull();
    expect(within(binding).getByText("Expert fields").closest("details")?.open).toBe(false);
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
