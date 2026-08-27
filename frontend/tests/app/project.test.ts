import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  CockpitRequestError,
  createCockpitApi,
  type AgentConfigurationRevisionListItem,
  type AuthProfileRevision,
  type CockpitApi,
  type ModelRegistryRevision,
  type Problem,
  type ProjectModelDefaultsRevision
} from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { THE_ONE_PROJECT } from "../../src/lib/project";
import { settingsPageCopy } from "../../src/lib/settingsPageCopy";
import { cockpitApiStub } from "../support/cockpitApi";

/** Live 2026-08-27 Settings payloads from GET http://127.0.0.1:8422, copied verbatim. */
const LIVE_AGENT_CONFIGURATION_REVISIONS =
  '{"items":[{"model":"claude-opus-4-6","auth_profile_revision_hash":"294db7c5313a29d936efc3684dbce0e85710bb22afb8aeace087310bd75732e8","executor_revision":"claude-atelier-doors/v1","provider_id":"anthropic","auth_mode":"subscription","requested_capability":"headless_with_tools","agent_configuration_revision_hash":"1a9498d43ace23b3cb9e56d374734b0a17648a5e092205b52497535a1749dfff","startable":true,"not_startable_reason":null},{"model":"grok-4.6","auth_profile_revision_hash":"dc5b676d2f6dca42984f6d5ceaefad58455b748a15cc85a08b1a476308f23616","executor_revision":"grok-subscription/v1","provider_id":"xai","auth_mode":"subscription","requested_capability":"headless","agent_configuration_revision_hash":"6ee1d546804f5f18eb6da493905e6eefce27d12a4f6af82b804ee2359b9ba7e0","startable":true,"not_startable_reason":null}],"next_after_revision_hash":null}';
const LIVE_AUTH_PROFILE_REVISIONS =
  '{"items":[{"profile_id":"operator-anthropic-subscription","revision_number":1,"provider_id":"anthropic","auth_mode":"subscription","auth_profile_revision_hash":"294db7c5313a29d936efc3684dbce0e85710bb22afb8aeace087310bd75732e8"},{"profile_id":"grok-felix","revision_number":1,"provider_id":"xai","auth_mode":"subscription","auth_profile_revision_hash":"dc5b676d2f6dca42984f6d5ceaefad58455b748a15cc85a08b1a476308f23616"}],"next_after_revision_hash":null}';
const LIVE_PROJECTS = '{"items":[{"public_project_reference":"project1.YXRlbGllcg"}]}';
const LIVE_SOURCE_CONNECTION =
  '{"public_project_reference":"project1.YXRlbGllcg","revision_number":2,"source_kind":"github","source_address":"FlexOr2/atelier-2@main","auth_method":"personal-access-token","project_source_connection_revision_hash":"a8e3ef4bf17dcb0262d2cc5ad2073133437a189a69d437f4b50c072fab31a7bc"}';
const LIVE_REGISTRY_XAI =
  '{"provider_id":"xai","revision_number":1,"model_registry_revision_hash":"fc2d1c4a3aeaf8233fb0c692168339ac355d911cad9933aaae367ac5c7637e21","entries":[{"model_id":"grok-4.6","agent_configuration_revision_hash":"6ee1d546804f5f18eb6da493905e6eefce27d12a4f6af82b804ee2359b9ba7e0","source":"discovered","provider_check":"checked"}]}';
const LIVE_REGISTRY_ANTHROPIC =
  '{"provider_id":"anthropic","revision_number":1,"model_registry_revision_hash":"ac511b61604fff9ca85de239f2b4e4c21c2bd9ef252fca5cfc66b4617fd0535f","entries":[{"model_id":"claude-opus-4-6","agent_configuration_revision_hash":"1a9498d43ace23b3cb9e56d374734b0a17648a5e092205b52497535a1749dfff","source":"operator","provider_check":"not-checked"}]}';
const LIVE_MODEL_DEFAULTS_MISSING =
  '{"type":"urn:atelier2:problem:v1:project-model-defaults-missing","title":"Project model defaults not found","status":404,"detail":"Choose the project\'s model defaults for difficulty 1, 2, and 3."}';
const LIVE_PROJECT_REFERENCE = "project1.YXRlbGllcg";
const LIVE_XAI_CONFIGURATION_HASH =
  "6ee1d546804f5f18eb6da493905e6eefce27d12a4f6af82b804ee2359b9ba7e0";
const LIVE_XAI_REGISTRY_HASH =
  "fc2d1c4a3aeaf8233fb0c692168339ac355d911cad9933aaae367ac5c7637e21";

const configurationHash = "a".repeat(64);
const profileHash = "b".repeat(64);
const registryHash = "c".repeat(64);
const defaultsHash = "d".repeat(64);
const projectReference = "project1.dGVzdA";

const configuration: AgentConfigurationRevisionListItem = {
  model: "claude-opus-4-1",
  auth_profile_revision_hash: profileHash,
  executor_revision: "v1",
  requested_capability: "headless",
  provider_id: "anthropic",
  auth_mode: "subscription",
  agent_configuration_revision_hash: configurationHash,
  startable: true,
  not_startable_reason: null
};

const profile: AuthProfileRevision = {
  profile_id: "Max account",
  revision_number: 1,
  provider_id: "anthropic",
  auth_mode: "subscription",
  auth_profile_revision_hash: profileHash
};

const registry = (entries: ModelRegistryRevision["entries"]): ModelRegistryRevision => ({
  provider_id: "anthropic",
  revision_number: 1,
  model_registry_revision_hash: registryHash,
  entries
});

const registeredEntry: ModelRegistryRevision["entries"][number] = {
  model_id: configuration.model,
  agent_configuration_revision_hash: configurationHash,
  source: "operator",
  provider_check: "checked"
};

const defaults = (items: ProjectModelDefaultsRevision["defaults"]): ProjectModelDefaultsRevision => ({
  project_id: "atelier",
  public_project_reference: projectReference,
  revision_number: 1,
  project_model_defaults_revision_hash: defaultsHash,
  defaults: items
});

const difficultyThree = {
  difficulty: 3 as const,
  model_registry_revision_hash: registryHash,
  provider_id: "anthropic",
  model_id: configuration.model,
  agent_configuration_revision_hash: configurationHash
};

beforeEach(() => {
  sessionStorage.clear();
  window.history.replaceState(null, "", "/atelier/settings");
});

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
});

function jsonResponse(body: string, status = 200, contentType = "application/json"): Response {
  return new Response(body, { status, headers: { "content-type": contentType } });
}

function liveSettingsFetcher(
  writes: { defaults: string[] } = { defaults: [] }
): typeof fetch {
  return async (input, init) => {
    const url = new URL(String(input), "http://atelier.test");
    const method = (init?.method ?? "GET").toUpperCase();
    if (method === "GET" && url.pathname === "/atelier/api/v1/projects") {
      return jsonResponse(LIVE_PROJECTS);
    }
    if (
      method === "GET"
      && url.pathname === `/atelier/api/v1/projects/${LIVE_PROJECT_REFERENCE}/source-connection`
    ) {
      return jsonResponse(LIVE_SOURCE_CONNECTION);
    }
    if (method === "GET" && url.pathname === "/atelier/api/v1/agent-configuration-revisions") {
      return jsonResponse(LIVE_AGENT_CONFIGURATION_REVISIONS);
    }
    if (method === "GET" && url.pathname === "/atelier/api/v1/auth-profile-revisions") {
      return jsonResponse(LIVE_AUTH_PROFILE_REVISIONS);
    }
    if (method === "GET" && url.pathname === "/atelier/api/v1/model-registries/xai") {
      return jsonResponse(LIVE_REGISTRY_XAI);
    }
    if (method === "GET" && url.pathname === "/atelier/api/v1/model-registries/anthropic") {
      return jsonResponse(LIVE_REGISTRY_ANTHROPIC);
    }
    if (
      method === "GET"
      && url.pathname === `/atelier/api/v1/projects/${LIVE_PROJECT_REFERENCE}/model-defaults`
    ) {
      return jsonResponse(LIVE_MODEL_DEFAULTS_MISSING, 404, "application/problem+json");
    }
    if (
      method === "PUT"
      && url.pathname === `/atelier/api/v1/projects/${LIVE_PROJECT_REFERENCE}/model-defaults`
    ) {
      const body = typeof init?.body === "string" ? init.body : "";
      writes.defaults.push(body);
      const input = JSON.parse(body) as {
        revision_number: number;
        defaults: ProjectModelDefaultsRevision["defaults"];
      };
      return jsonResponse(JSON.stringify({
        project_id: "atelier",
        public_project_reference: LIVE_PROJECT_REFERENCE,
        revision_number: input.revision_number,
        project_model_defaults_revision_hash: "d".repeat(64),
        defaults: input.defaults
      }), 201);
    }
    throw new Error(`unmocked ${method} ${url.pathname}`);
  };
}

function modelRegistryMissing(): CockpitRequestError {
  return new CockpitRequestError("missing", {
    type: "urn:atelier2:problem:v1:model-registry-missing",
    title: "Model registry not found",
    status: 404,
    detail: "Publish a model-registry revision for this provider."
  } as Problem, true);
}

function openSettings(overrides: Partial<CockpitApi> = {}) {
  return render(App, {
    props: {
      cockpitApi: cockpitApiStub({
        listProjects: vi.fn(async () => ({ items: [{ public_project_reference: projectReference }] })),
        getProjectSourceConnection: vi.fn(async () => ({
          public_project_reference: projectReference,
          revision_number: 2,
          source_kind: "github",
          source_address: "atelier/atelier-2",
          auth_method: "personal-access-token" as const,
          project_source_connection_revision_hash: "e".repeat(64)
        })),
        listAgentConfigurationRevisions: vi.fn(async () => ({
          items: [configuration],
          next_after_revision_hash: null
        })),
        listAuthProfileRevisions: vi.fn(async () => ({
          items: [profile],
          next_after_revision_hash: null
        })),
        getModelRegistry: vi.fn(async () => registry([registeredEntry])),
        getProjectModelDefaults: vi.fn(async () => defaults([difficultyThree])),
        ...overrides
      }),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
}

describe("Settings owns project sources, models, and defaults", () => {
  it("renders the v8 order, exact registry evidence, and no run counts", async () => {
    openSettings();

    expect((await screen.findByRole("heading", { name: THE_ONE_PROJECT })).isConnected).toBe(true);
    const sources = await screen.findByRole("heading", { name: settingsPageCopy.sourcesTitle });
    const models = screen.getByRole("heading", { name: settingsPageCopy.modelsTitle });
    const modelDefaults = screen.getByRole("heading", { name: settingsPageCopy.defaultsTitle });
    expect(sources.compareDocumentPosition(models) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
    expect(models.compareDocumentPosition(modelDefaults) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
    expect(screen.queryByText(/Work in this project/)).toBeNull();
    expect(screen.getByText(configuration.model)).toBeTruthy();
    expect(screen.getByText(profile.profile_id)).toBeTruthy();
    expect(screen.getByText("added by you · ✓ checked")).toBeTruthy();
    expect(screen.queryByText("✓ startable")).toBeNull();
    expect(screen.getAllByRole("combobox").map((item) => item.getAttribute("aria-label"))).toEqual([
      "Difficulty 3",
      "Difficulty 2",
      "Difficulty 1"
    ]);
    expect(screen.queryByText(/Saving|Saved/)).toBeNull();
  });

  it("writes registry membership immediately with frozen exact bytes", async () => {
    const putModelRegistry = vi.fn(async (_providerId, write) => ({
      status: 200,
      value: { ...registry(write.input.entries), revision_number: write.input.revision_number }
    }));
    openSettings({
      getModelRegistry: vi.fn(async () => registry([])),
      getProjectModelDefaults: vi.fn(async () => defaults([])),
      putModelRegistry
    });

    await fireEvent.change(await screen.findByRole("combobox", { name: "Add a model" }), {
      target: { value: configurationHash }
    });

    await waitFor(() => expect(putModelRegistry).toHaveBeenCalledTimes(1));
    const [, write] = putModelRegistry.mock.calls[0] ?? [];
    expect(write.body).toBe(JSON.stringify(write.input));
    expect(write.input).toEqual({
      revision_number: 2,
      entries: [{
        model_id: registeredEntry.model_id,
        agent_configuration_revision_hash: registeredEntry.agent_configuration_revision_hash
      }]
    });
  });

  it("keeps an unavailable saved default visible until it is cleared", async () => {
    openSettings({
      listAgentConfigurationRevisions: vi.fn(async () => ({
        items: [{
          ...configuration,
          startable: false,
          not_startable_reason: "agent-executor-binding-unavailable" as const
        }],
        next_after_revision_hash: null
      })),
      getModelRegistry: vi.fn(async () => registry([{
        ...registeredEntry,
        provider_check: "unknown-at-provider"
      }]))
    });

    expect((await screen.findByText("◇ unknown at provider")).isConnected).toBe(true);
    expect(screen.getByText(settingsPageCopy.defaultsUnavailableModels)).toBeTruthy();
    expect(screen.getByText(`${configuration.model} · Account ${profile.profile_id} — Unavailable`)).toBeTruthy();
    const difficultyThree = screen.getByRole("combobox", { name: "Difficulty 3" });
    expect(difficultyThree).toHaveProperty("value", "");
    expect(within(difficultyThree).getByRole("option", { name: "Change saved default" })).toBeTruthy();
    expect(screen.getByRole("combobox", { name: "Difficulty 2" })).toBeTruthy();
    expect(screen.getByRole("combobox", { name: "Difficulty 1" })).toBeTruthy();
    expect(screen.queryByText("✓ checked")).toBeNull();
    expect(screen.queryByText("✓ startable")).toBeNull();
  });

  it("clearing a default writes the remaining defaults immediately", async () => {
    const retained = {
      difficulty: 1 as const,
      model_registry_revision_hash: "1".repeat(64),
      provider_id: "openai",
      model_id: "gpt-retained",
      agent_configuration_revision_hash: "2".repeat(64)
    };
    const putProjectModelDefaults = vi.fn(async (_reference, write) => ({
      status: 200,
      value: { ...defaults(write.input.defaults), revision_number: write.input.revision_number }
    }));
    openSettings({
      getProjectModelDefaults: vi.fn(async () => defaults([difficultyThree, retained])),
      putProjectModelDefaults
    });

    await fireEvent.change(await screen.findByRole("combobox", { name: "Difficulty 3" }), {
      target: { value: "" }
    });

    await waitFor(() => expect(putProjectModelDefaults).toHaveBeenCalledTimes(1));
    const [, write] = putProjectModelDefaults.mock.calls[0] ?? [];
    expect(write.input).toEqual({ revision_number: 2, defaults: [retained] });
    expect(write.body).toBe(JSON.stringify(write.input));
  });

  it("replaces one difficulty while carrying the saved sibling bytes unchanged", async () => {
    const retained = {
      difficulty: 1 as const,
      model_registry_revision_hash: "1".repeat(64),
      provider_id: "openai",
      model_id: "gpt-retained",
      agent_configuration_revision_hash: "2".repeat(64)
    };
    const putProjectModelDefaults = vi.fn(async (_reference, write) => ({
      status: 200,
      value: { ...defaults(write.input.defaults), revision_number: write.input.revision_number }
    }));
    openSettings({
      getProjectModelDefaults: vi.fn(async () => defaults([retained])),
      putProjectModelDefaults
    });

    await fireEvent.change(await screen.findByRole("combobox", { name: "Difficulty 2" }), {
      target: { value: configurationHash }
    });

    await waitFor(() => expect(putProjectModelDefaults).toHaveBeenCalledTimes(1));
    const [, write] = putProjectModelDefaults.mock.calls[0] ?? [];
    expect(write.input.defaults).toEqual([retained, { ...difficultyThree, difficulty: 2 }]);
  });

  it("leaves unregistered configurations behind Add a model", async () => {
    openSettings({
      getModelRegistry: vi.fn(async () => registry([])),
      getProjectModelDefaults: vi.fn(async () => defaults([]))
    });

    const addModel = await screen.findByRole("combobox", { name: "Add a model" });
    expect(within(addModel).getByRole("option", { name: new RegExp(configuration.model) })).toBeTruthy();
    expect(within(screen.getByRole("combobox", { name: "Difficulty 3" })).queryByRole(
      "option",
      { name: new RegExp(configuration.model) }
    )).toBeNull();
  });

  it("checks an operator model before offering it as a default", async () => {
    const validateModelRegistryEntry = vi.fn(async () => ({
      status: 201,
      value: {
        ...registry([{ ...registeredEntry, provider_check: "checked" as const }]),
        revision_number: 2
      }
    }));
    openSettings({
      getModelRegistry: vi.fn(async () => registry([{
        ...registeredEntry,
        provider_check: "not-checked"
      }])),
      getProjectModelDefaults: vi.fn(async () => defaults([])),
      validateModelRegistryEntry
    });

    expect(await screen.findByText(settingsPageCopy.defaultsNoCheckedModels)).toBeTruthy();
    expect(within(screen.getByRole("combobox", { name: "Difficulty 2" })).queryByRole(
      "option",
      { name: new RegExp(configuration.model) }
    )).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: "Check" }));

    await waitFor(() => expect(validateModelRegistryEntry).toHaveBeenCalledWith(
      "anthropic",
      configurationHash
    ));
    expect(within(screen.getByRole("combobox", { name: "Difficulty 2" })).getByRole(
      "option",
      { name: new RegExp(configuration.model) }
    )).toBeTruthy();
  });

  it("renders a startable provider with no registry as unavailable with Check", async () => {
    const putModelRegistry = vi.fn(async (_providerId, write) => ({
      status: 201,
      value: {
        ...registry([{ ...registeredEntry, provider_check: "not-checked" as const }]),
        revision_number: write.input.revision_number,
        model_registry_revision_hash: "f".repeat(64)
      }
    }));
    const validateModelRegistryEntry = vi.fn(async () => ({
      status: 201,
      value: {
        ...registry([{ ...registeredEntry, provider_check: "checked" as const }]),
        revision_number: 2,
        model_registry_revision_hash: "f".repeat(64)
      }
    }));
    openSettings({
      getModelRegistry: vi.fn(async () => { throw modelRegistryMissing(); }),
      getProjectModelDefaults: vi.fn(async () => defaults([])),
      putModelRegistry,
      validateModelRegistryEntry
    });

    expect(await screen.findByText("anthropic")).toBeTruthy();
    expect(screen.getByText(configuration.model)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Check" })).toBeTruthy();
    expect(screen.getByText(settingsPageCopy.defaultsNoCheckedModels)).toBeTruthy();
    expect(screen.queryByRole("combobox", { name: "Add a model" })).toBeNull();
    expect(within(screen.getByRole("combobox", { name: "Difficulty 3" })).queryByRole(
      "option",
      { name: new RegExp(configuration.model) }
    )).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Check" }));

    await waitFor(() => {
      expect(putModelRegistry).toHaveBeenCalledTimes(1);
      expect(validateModelRegistryEntry).toHaveBeenCalledWith("anthropic", configurationHash);
      expect(within(screen.getByRole("combobox", { name: "Difficulty 3" })).getByRole(
        "option",
        { name: new RegExp(configuration.model) }
      )).toBeTruthy();
    });
  });

  it("lists every live startable model and writes the first default through the real decoder", async () => {
    const writes = { defaults: [] as string[] };
    const api = createCockpitApi(liveSettingsFetcher(writes));
    const putProjectModelDefaults = vi.fn(api.putProjectModelDefaults);
    const validateModelRegistryEntry = vi.fn(api.validateModelRegistryEntry);
    render(App, {
      props: {
        cockpitApi: { ...api, putProjectModelDefaults, validateModelRegistryEntry },
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    expect((await screen.findByText("anthropic")).isConnected).toBe(true);
    expect(screen.getByText("xai")).toBeTruthy();
    expect(screen.getByText("claude-opus-4-6")).toBeTruthy();
    expect(screen.getByText("operator-anthropic-subscription")).toBeTruthy();
    expect(screen.getByText("grok-4.6")).toBeTruthy();
    expect(screen.getByText("grok-felix")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Check" })).toBeTruthy();
    expect(screen.queryByRole("combobox", { name: "Add a model" })).toBeNull();

    for (const difficulty of [3, 2, 1]) {
      const select = screen.getByRole("combobox", { name: `Difficulty ${difficulty}` });
      expect(within(select).queryByRole("option", {
        name: "claude-opus-4-6 · Account operator-anthropic-subscription"
      })).toBeNull();
      expect(within(select).getByRole("option", {
        name: "grok-4.6 · Account grok-felix"
      })).toBeTruthy();
    }

    await fireEvent.change(screen.getByRole("combobox", { name: "Difficulty 3" }), {
      target: { value: LIVE_XAI_CONFIGURATION_HASH }
    });

    await waitFor(() => expect(putProjectModelDefaults).toHaveBeenCalledTimes(1));
    expect(validateModelRegistryEntry).not.toHaveBeenCalled();
    const write = putProjectModelDefaults.mock.calls[0]?.[1];
    if (write === undefined) throw new Error("expected a defaults write");
    expect(write.body).toBe(JSON.stringify(write.input));
    expect(write.input).toEqual({
      revision_number: 1,
      defaults: [{
        difficulty: 3,
        model_registry_revision_hash: LIVE_XAI_REGISTRY_HASH,
        provider_id: "xai",
        model_id: "grok-4.6",
        agent_configuration_revision_hash: LIVE_XAI_CONFIGURATION_HASH
      }]
    });
    expect(writes.defaults).toEqual([write.body]);
  });

  it("does not promise an absent add control for an empty registry", async () => {
    openSettings({
      listAgentConfigurationRevisions: vi.fn(async () => ({
        items: [],
        next_after_revision_hash: null
      })),
      listAuthProfileRevisions: vi.fn(async () => ({
        items: [],
        next_after_revision_hash: null
      })),
      getProjectModelDefaults: vi.fn(async () => defaults([]))
    });

    expect(await screen.findByText(settingsPageCopy.modelsEmpty)).toBeTruthy();
    expect(screen.getByText(settingsPageCopy.defaultsEmptyRegistry)).toBeTruthy();
    expect(screen.queryByRole("combobox", { name: "Add a model" })).toBeNull();
    expect(screen.queryByText(/add a model/i)).toBeNull();
  });

  it("retries the exact failed defaults write", async () => {
    const putProjectModelDefaults = vi
      .fn()
      .mockRejectedValueOnce(new Error("uncertain"))
      .mockImplementationOnce(async (_reference, write) => ({
        status: 200,
        value: { ...defaults(write.input.defaults), revision_number: write.input.revision_number }
      }));
    openSettings({ putProjectModelDefaults });

    await fireEvent.change(await screen.findByRole("combobox", { name: "Difficulty 3" }), {
      target: { value: "" }
    });
    await fireEvent.click(await screen.findByRole("button", { name: "Retry" }));

    await waitFor(() => expect(putProjectModelDefaults).toHaveBeenCalledTimes(2));
    expect(putProjectModelDefaults.mock.calls[1]).toEqual(putProjectModelDefaults.mock.calls[0]);
  });

  it("retries the identical uncertain registry write", async () => {
    const putModelRegistry = vi
      .fn()
      .mockRejectedValueOnce(new Error("uncertain"))
      .mockImplementationOnce(async (_providerId, write) => ({
        status: 200,
        value: { ...registry(write.input.entries), revision_number: write.input.revision_number }
      }));
    openSettings({ putModelRegistry });

    await fireEvent.click(await screen.findByRole("button", { name: "Remove" }));
    await fireEvent.click(await screen.findByRole("button", { name: "Retry" }));

    await waitFor(() => expect(putModelRegistry).toHaveBeenCalledTimes(2));
    expect(putModelRegistry.mock.calls[1]).toEqual(putModelRegistry.mock.calls[0]);
  });

  it("shows honest empty and failed states", async () => {
    const view = openSettings({
      listAgentConfigurationRevisions: vi.fn(async () => ({
        items: [],
        next_after_revision_hash: null
      })),
      listAuthProfileRevisions: vi.fn(async () => ({ items: [], next_after_revision_hash: null })),
      getProjectModelDefaults: vi.fn(async () => defaults([]))
    });
    expect((await screen.findByText(settingsPageCopy.modelsEmpty)).isConnected).toBe(true);
    view.unmount();

    openSettings({ listProjects: vi.fn(async () => { throw new Error("private"); }) });
    const failure = await screen.findByRole("alert");
    expect(within(failure).getByText(settingsPageCopy.unavailable)).toBeTruthy();
    expect(screen.queryByText("private")).toBeNull();
  });

  it("proves(settings-preserves-confirmed-truth-and-retries-only-its-failed-read): retries the failed Settings snapshot as one read", async () => {
    const listProjects = vi
      .fn()
      .mockRejectedValueOnce(new Error("private"))
      .mockResolvedValueOnce({ items: [{ public_project_reference: projectReference }] });
    openSettings({ listProjects });

    await fireEvent.click(await screen.findByRole("button", { name: "Retry settings" }));

    expect((await screen.findByRole("heading", { name: settingsPageCopy.sourcesTitle })).isConnected).toBe(true);
    expect(listProjects).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("private")).toBeNull();
  });

  it("proves(settings-reads-every-model-configuration-page-or-says-it-could-not): reads model configurations through the final page", async () => {
    const next = "f".repeat(64);
    const listAgentConfigurationRevisions = vi
      .fn()
      .mockResolvedValueOnce({ items: [], next_after_revision_hash: next })
      .mockResolvedValueOnce({ items: [configuration], next_after_revision_hash: null });
    openSettings({ listAgentConfigurationRevisions });

    expect((await screen.findByText(configuration.model)).isConnected).toBe(true);
    expect(listAgentConfigurationRevisions.mock.calls).toEqual([[undefined], [next]]);
  });

  it("carries no local trail back from the rail destination", async () => {
    openSettings();
    await screen.findByRole("heading", { name: THE_ONE_PROJECT });
    expect(screen.queryByRole("navigation", { name: "Where you are" })).toBeNull();
  });
});
