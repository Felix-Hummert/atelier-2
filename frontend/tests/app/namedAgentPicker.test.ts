import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  agentConfigurationRevisionPageSchema,
  type AgentConfigurationRevisionListItem,
  type AuthProfileRevision,
  type CockpitApi,
  type ModelRegistryRevision,
  type ProjectModelResolution,
  type RunV3
} from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { agentConfigurationLabel } from "../../src/lib/agentConfigurationLabel";
import { cockpitApiStub } from "../support/cockpitApi";
import { cancellableBlock } from "../support/runV3";

const revisionHash = "a".repeat(64);
const authHash = "b".repeat(64);
const configurationHash = "c".repeat(64);
const publicReference = "run1.cnVuLW5hbWVk";
const projectReference = "project1.dGVzdA";

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

function v3Revision(hash: string, documentBase64: string, roles = ["builder"]) {
  return {
    workflow_revision_hash: hash,
    document_base64: documentBase64,
    graph: {
      workflow_format_version: 3 as const,
      executable: true as const,
      not_executable_reason: null,
      node_count: roles.length,
      agent_roles: roles,
      orders: [],
      wait_answer_schemas: [],
      node_previews: roles.map((role, index) => ({
          id: `step-${index + 1}`,
          kind: "agent" as const,
          role,
          instruction_start: "Do the one thing.",
          depends_on: index === 0 ? [] : [`step-${index}`]
        })),
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
    orders: [],
    state_version: 1,
    state: "STARTED",
    current_node_id: "implement",
    node_rail: [{ node_id: "implement", state: "working", attempt: null }],
    cancellation: cancellableBlock(),
    terminal_hash: null,
    latest_event_cursor: null
  };
}

function listedProfile(): AuthProfileRevision {
  return {
    profile_id: "max",
    revision_number: 1,
    provider_id: "anthropic",
    auth_mode: "subscription",
    auth_profile_revision_hash: authHash
  };
}

function registry(providerId: string): ModelRegistryRevision {
  const entries = providerId === "anthropic"
    ? [{
        model_id: "sonnet",
        agent_configuration_revision_hash: configurationHash,
        source: "discovered" as const,
        provider_check: "checked" as const
      }]
    : providerId === "openai"
      ? [{
          model_id: "codex",
          agent_configuration_revision_hash: "d".repeat(64),
          source: "discovered" as const,
          provider_check: "checked" as const
        }]
      : [];
  return {
    provider_id: providerId,
    revision_number: 1,
    model_registry_revision_hash: "e".repeat(64),
    entries
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
    listProjects: vi.fn(async () => ({ items: [{ public_project_reference: projectReference }] })),
    listAuthProfileRevisions: vi.fn(async () => ({
      items: [listedProfile()],
      next_after_revision_hash: null
    })),
    getModelRegistry: vi.fn(async (providerId: string) => registry(providerId)),
    resolveProjectModels: vi.fn(async (
      _project: string,
      workflowHash: string,
      modelOverrides: Parameters<CockpitApi["resolveProjectModels"]>[2]
    ) => {
      const selected = modelOverrides.find((item) => item.role === "builder")
        ?.agent_configuration_revision_hash ?? null;
      return modelResolution(workflowHash, [{
        role: "builder",
        hash: selected,
        source: selected === null ? "uncast" : "chosen-now",
        uncastReason: selected === null ? "no-project-default" : null
      }]);
    }),
    ...overrides
  });
}

function modelResolution(
  workflowHash: string,
  bindings: Array<{
    role: string;
    hash: string | null;
    source: "chosen-now" | "pinned-in-workflow" | "from-project" | "uncast";
    uncastReason: ProjectModelResolution["resolutions"][number]["uncast_reason"];
    defaultDifficulty?: 1 | 2 | 3 | null;
    familyDiffersFrom?: string | null;
  }>
): ProjectModelResolution {
  return {
    project_id: "atelier",
    public_project_reference: projectReference,
    workflow_revision_hash: workflowHash,
    resolutions: bindings.map((binding) => ({
      role: binding.role,
      agent_configuration_revision_hash: binding.hash,
      source: binding.source,
      model_id: binding.hash === null ? null : "sonnet",
      declared_difficulty: 2,
      default_difficulty: binding.defaultDifficulty ?? (binding.source === "from-project" ? 2 : null),
      uncast_reason: binding.uncastReason,
      family_differs_from: binding.familyDiffersFrom ?? null
    }))
  };
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
    expect(picker.textContent).toContain("anthropic · sonnet · max");
    expect(picker.textContent).toContain("openai · codex · max");
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

  it("offers a registered exact model with its account, and starts with that hash", async () => {
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
    expect(agentConfigurationLabel(agent)).toBe("anthropic · sonnet · Subscription");
    expect(picker.textContent).toContain("anthropic · sonnet · max");
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
  });

  it("re-resolves at Start and refuses a project default removed after preview", async () => {
    const agent = publishedAgent();
    const resolveProjectModels = vi
      .fn()
      .mockResolvedValueOnce(modelResolution(revisionHash, [{
        role: "builder",
        hash: configurationHash,
        source: "from-project",
        uncastReason: null
      }]))
      .mockResolvedValueOnce(modelResolution(revisionHash, [{
        role: "builder",
        hash: null,
        source: "uncast",
        uncastReason: "no-project-default"
      }]));
    const cockpitApi = api({
      listAgentConfigurationRevisions: vi.fn(async () => ({
        items: [agent],
        next_after_revision_hash: null
      })),
      resolveProjectModels
    });
    await publishWorkflow(cockpitApi);

    const binding = await screen.findByRole("article", { name: "Binding builder" });
    await waitFor(() => expect(within(binding).getByLabelText("Agent for builder")).toHaveProperty(
      "value",
      configurationHash
    ));
    expect(within(binding).getByText("From project")).toBeTruthy();

    await fireEvent.click(screen.getByRole("button", { name: "Start" }));

    await waitFor(() => expect(resolveProjectModels).toHaveBeenCalledTimes(2));
    expect(resolveProjectModels.mock.calls.map((call) => call[2])).toEqual([[], []]);
    expect(cockpitApi.start).not.toHaveBeenCalled();
    await waitFor(() => expect(within(binding).getByLabelText("Agent for builder")).toHaveProperty(
      "value",
      ""
    ));
    expect(within(binding).getByLabelText("Binding source: Choose")).toBeTruthy();
    expect(within(binding).queryByRole("alert")).toBeNull();
    expect(binding.classList).not.toContain("node-needs_you");
  });

  it("names the refused override on its role and sends no start request", async () => {
    const rejectedHash = "d".repeat(64);
    const resolveProjectModels = vi
      .fn()
      .mockResolvedValueOnce(modelResolution(revisionHash, [{
        role: "builder",
        hash: configurationHash,
        source: "from-project",
        uncastReason: null
      }]))
      .mockResolvedValueOnce(modelResolution(revisionHash, [{
        role: "builder",
        hash: rejectedHash,
        source: "chosen-now",
        uncastReason: null
      }]))
      .mockResolvedValueOnce(modelResolution(revisionHash, [{
        role: "builder",
        hash: null,
        source: "uncast",
        uncastReason: "override-not-registered"
      }]));
    const cockpitApi = api({
      listAgentConfigurationRevisions: vi.fn(async () => ({
        items: [
          publishedAgent(),
          publishedAgent({
            agent_configuration_revision_hash: rejectedHash,
            provider_id: "openai",
            model: "codex"
          })
        ],
        next_after_revision_hash: null
      })),
      resolveProjectModels
    });
    await publishWorkflow(cockpitApi);

    const builder = await screen.findByRole("article", { name: "Binding builder" });
    await waitFor(() => expect(within(builder).getByLabelText("Agent for builder")).toHaveProperty(
      "value",
      configurationHash
    ));
    await fireEvent.change(within(builder).getByLabelText("Agent for builder"), {
      target: { value: rejectedHash }
    });
    await waitFor(() => expect(within(builder).getByText("Chosen now")).toBeTruthy());

    await fireEvent.click(screen.getByRole("button", { name: "Start" }));

    const why = await within(builder).findByRole("button", {
      name: "Why builder needs a choice"
    });
    expect(within(builder).queryByRole("alert")).toBeNull();
    await fireEvent.click(why);
    expect(within(builder).getByRole("status").textContent).toBe(
      "Override not registered."
    );
    expect(cockpitApi.start).not.toHaveBeenCalled();
  });

  it("names the family relation on the refused role and sends no start request", async () => {
    const reviewerHash = "d".repeat(64);
    const refusal = () => modelResolution(revisionHash, [
      {
        role: "builder",
        hash: configurationHash,
        source: "from-project" as const,
        uncastReason: null
      },
      {
        role: "reviewer",
        hash: null,
        source: "uncast" as const,
        uncastReason: "family-difference-unavailable" as const,
        familyDiffersFrom: "builder"
      }
    ]);
    const cockpitApi = api({
      publish: vi.fn(async (mutation) => ({
        status: 201,
        value: v3Revision(
          mutation.mutation_id.slice("publish:".length),
          mutation.body_base64,
          ["builder", "reviewer"]
        )
      }) as never),
      listAgentConfigurationRevisions: vi.fn(async () => ({
        items: [
          publishedAgent(),
          publishedAgent({
            agent_configuration_revision_hash: reviewerHash,
            provider_id: "openai",
            model: "codex"
          })
        ],
        next_after_revision_hash: null
      })),
      resolveProjectModels: vi.fn(async () => refusal())
    });
    await publishWorkflow(cockpitApi);

    const builder = await screen.findByRole("article", { name: "Binding builder" });
    const reviewer = screen.getByRole("article", { name: "Binding reviewer" });
    await waitFor(() => expect(within(builder).getByText("From project")).toBeTruthy());
    const why = within(reviewer).getByRole("button", {
      name: "Why reviewer needs a choice"
    });
    expect(within(reviewer).queryByRole("alert")).toBeNull();
    await fireEvent.click(why);
    expect(within(reviewer).getByRole("status").textContent).toBe(
      "Family difference from builder unavailable."
    );

    await fireEvent.click(screen.getByRole("button", { name: "Start" }));

    await waitFor(() => expect(cockpitApi.resolveProjectModels).toHaveBeenCalledTimes(2));
    expect(await within(reviewer).findByRole("button", {
      name: "Why reviewer needs a choice"
    })).toBeTruthy();
    expect(within(reviewer).queryByRole("alert")).toBeNull();
    expect(cockpitApi.start).not.toHaveBeenCalled();
  });

  it("accepts a full resolved V3 response for a start request with no manual overrides", async () => {
    const agent = publishedAgent();
    const cockpitApi = api({
      listAgentConfigurationRevisions: vi.fn(async () => ({
        items: [agent],
        next_after_revision_hash: null
      })),
      start: vi.fn(async (mutation) => ({
        status: 201,
        value: {
          ...startedV3Run(),
          workflow_revision_hash: JSON.parse(globalThis.atob(mutation.body_base64)).workflow_revision_hash
        }
      }) as never),
      resolveProjectModels: vi.fn(async (_project, workflowHash) => modelResolution(workflowHash, [{
        role: "builder",
        hash: configurationHash,
        source: "from-project",
        uncastReason: null
      }]))
    });
    await publishWorkflow(cockpitApi);

    const binding = await screen.findByRole("article", { name: "Binding builder" });
    await waitFor(() => expect(within(binding).getByLabelText("Agent for builder")).toHaveProperty(
      "value",
      configurationHash
    ));
    await fireEvent.click(screen.getByRole("button", { name: "Start" }));

    await waitFor(() => expect(cockpitApi.getRun).toHaveBeenCalledTimes(1));
    const body = JSON.parse(globalThis.atob(vi.mocked(cockpitApi.start).mock.calls[0]?.[0].body_base64 ?? ""));
    expect(body.agent_bindings).toEqual([]);
  });

  it("submits only the manual override and shows the family-rebalanced recommendation", async () => {
    const reviewerHash = "d".repeat(64);
    const builder = publishedAgent();
    const reviewer = publishedAgent({
      agent_configuration_revision_hash: reviewerHash,
      provider_id: "openai",
      model: "codex"
    });
    const resolveProjectModels = vi.fn(async (
      _project: string,
      workflowHash: string,
      modelOverrides: Parameters<CockpitApi["resolveProjectModels"]>[2]
    ) => {
      const builderOverride = modelOverrides.find((item) => item.role === "builder")
        ?.agent_configuration_revision_hash;
      return modelResolution(workflowHash, builderOverride === undefined
        ? [
            { role: "builder", hash: configurationHash, source: "from-project", uncastReason: null },
            { role: "reviewer", hash: reviewerHash, source: "from-project", uncastReason: null }
          ]
        : [
            { role: "builder", hash: builderOverride, source: "chosen-now", uncastReason: null },
            { role: "reviewer", hash: configurationHash, source: "from-project", uncastReason: null }
          ]);
    });
    const cockpitApi = api({
      publish: vi.fn(async (mutation) => ({
        status: 201,
        value: v3Revision(
          mutation.mutation_id.slice("publish:".length),
          mutation.body_base64,
          ["builder", "reviewer"]
        )
      }) as never),
      listAgentConfigurationRevisions: vi.fn(async () => ({
        items: [builder, reviewer],
        next_after_revision_hash: null
      })),
      start: vi.fn(async (mutation) => ({
        status: 201,
        value: {
          ...startedV3Run(),
          workflow_revision_hash: JSON.parse(globalThis.atob(mutation.body_base64)).workflow_revision_hash,
          agent_bindings: [{
            ...startedV3Run().agent_bindings[0],
            agent_configuration_revision_hash: reviewerHash
          }]
        }
      }) as never),
      resolveProjectModels
    });
    await publishWorkflow(cockpitApi);

    const builderBinding = await screen.findByRole("article", { name: "Binding builder" });
    const reviewerBinding = screen.getByRole("article", { name: "Binding reviewer" });
    await waitFor(() => expect(within(reviewerBinding).getByLabelText("Agent for reviewer")).toHaveProperty(
      "value",
      reviewerHash
    ));

    await fireEvent.change(within(builderBinding).getByLabelText("Agent for builder"), {
      target: { value: reviewerHash }
    });

    await waitFor(() => expect(within(reviewerBinding).getByLabelText("Agent for reviewer")).toHaveProperty(
      "value",
      configurationHash
    ));
    expect(within(builderBinding).getByText("Chosen now")).toBeTruthy();
    expect(within(reviewerBinding).getByText("From project")).toBeTruthy();
    expect(resolveProjectModels.mock.calls.at(-1)?.[2]).toEqual([{
      role: "builder",
      agent_configuration_revision_hash: reviewerHash
    }]);

    await fireEvent.click(screen.getByRole("button", { name: "Start" }));

    await waitFor(() => expect(cockpitApi.start).toHaveBeenCalledTimes(1));
    expect(resolveProjectModels.mock.calls.at(-1)?.[2]).toEqual([{
      role: "builder",
      agent_configuration_revision_hash: reviewerHash
    }]);
    const body = JSON.parse(globalThis.atob(vi.mocked(cockpitApi.start).mock.calls[0]?.[0].body_base64 ?? ""));
    expect(body.agent_bindings).toEqual([{
      role: "builder",
      agent_configuration_revision_hash: reviewerHash
    }]);
    await waitFor(() => expect(cockpitApi.getRun).toHaveBeenCalledTimes(1));
  });

  it("shows next-higher provenance while unregistered configurations stay behind Add model", async () => {
    const agent = publishedAgent();
    const unregisteredHash = "d".repeat(64);
    const resolveProjectModels = vi.fn(async (
      _project: string,
      workflowHash: string,
      modelOverrides: Parameters<CockpitApi["resolveProjectModels"]>[2]
    ) => modelResolution(workflowHash, [modelOverrides.length === 0
      ? {
          role: "builder",
          hash: configurationHash,
          source: "from-project",
          uncastReason: null,
          defaultDifficulty: 3
        }
      : {
          role: "builder",
          hash: null,
          source: "uncast",
          uncastReason: "override-not-registered"
        }
    ]));
    const cockpitApi = api({
      listAgentConfigurationRevisions: vi.fn(async () => ({
        items: [agent, publishedAgent({ agent_configuration_revision_hash: unregisteredHash, model: "other" })],
        next_after_revision_hash: null
      })),
      resolveProjectModels
    });
    await publishWorkflow(cockpitApi);

    const binding = await screen.findByRole("article", { name: "Binding builder" });
    await waitFor(() => expect(within(binding).getByText("Next higher")).toBeTruthy());

    const picker = within(binding).getByLabelText("Agent for builder");
    expect(picker.textContent).toContain("anthropic · sonnet · max");
    expect(picker.textContent).not.toContain("anthropic · other · max");
    expect(within(binding).getByText("Expert fields")).toBeTruthy();
    expect(resolveProjectModels).toHaveBeenCalledTimes(1);
  });

  it("never offers a startable registry entry the provider marked unknown", async () => {
    const agent = publishedAgent();
    const cockpitApi = api({
      listAgentConfigurationRevisions: vi.fn(async () => ({
        items: [agent],
        next_after_revision_hash: null
      })),
      getModelRegistry: vi.fn(async () => ({
        ...registry("anthropic"),
        entries: [{
          model_id: "sonnet",
          agent_configuration_revision_hash: configurationHash,
          source: "operator" as const,
          provider_check: "unknown-at-provider" as const
        }]
      })),
      resolveProjectModels: vi.fn(async (_project, workflowHash) => modelResolution(workflowHash, [{
        role: "builder",
        hash: null,
        source: "uncast",
        uncastReason: "no-project-default"
      }]))
    });
    await publishWorkflow(cockpitApi);

    const binding = await screen.findByRole("article", { name: "Binding builder" });
    expect(within(binding).queryByLabelText("Agent for builder")).toBeNull();
    expect(within(binding).getByText("Expert fields")).toBeTruthy();
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
