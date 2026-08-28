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
  type ProjectModelDefaultsRevision,
  type ProjectSourceResource
} from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { THE_ONE_PROJECT } from "../../src/lib/project";
import { backLinkCopy } from "../../src/lib/backLinkCopy";
import { retryLabel } from "../../src/lib/readStateCopy";
import {
  accountChoice,
  difficultyLabel,
  noSuchModel,
  providerAccount,
  retainedAccountChoice,
  settingsPageCopy
} from "../../src/lib/settingsPageCopy";
import { cockpitApiStub } from "../support/cockpitApi";

/** Live 2026-08-27 Settings payloads from GET http://127.0.0.1:8422, copied verbatim. */
const LIVE_AGENT_CONFIGURATION_REVISIONS =
  '{"items":[{"model":"claude-opus-4-6","auth_profile_revision_hash":"294db7c5313a29d936efc3684dbce0e85710bb22afb8aeace087310bd75732e8","executor_revision":"claude-atelier-doors/v1","provider_id":"anthropic","auth_mode":"subscription","requested_capability":"headless_with_tools","agent_configuration_revision_hash":"1a9498d43ace23b3cb9e56d374734b0a17648a5e092205b52497535a1749dfff","startable":true,"not_startable_reason":null},{"model":"grok-4.6","auth_profile_revision_hash":"dc5b676d2f6dca42984f6d5ceaefad58455b748a15cc85a08b1a476308f23616","executor_revision":"grok-subscription/v1","provider_id":"xai","auth_mode":"subscription","requested_capability":"headless","agent_configuration_revision_hash":"6ee1d546804f5f18eb6da493905e6eefce27d12a4f6af82b804ee2359b9ba7e0","startable":true,"not_startable_reason":null}],"next_after_revision_hash":null}';
const LIVE_AUTH_PROFILE_REVISIONS =
  '{"items":[{"profile_id":"operator-anthropic-subscription","revision_number":1,"provider_id":"anthropic","auth_mode":"subscription","auth_profile_revision_hash":"294db7c5313a29d936efc3684dbce0e85710bb22afb8aeace087310bd75732e8"},{"profile_id":"grok-felix","revision_number":1,"provider_id":"xai","auth_mode":"subscription","auth_profile_revision_hash":"dc5b676d2f6dca42984f6d5ceaefad58455b748a15cc85a08b1a476308f23616"}],"next_after_revision_hash":null}';
const LIVE_PROJECTS = '{"items":[{"public_project_reference":"project1.YXRlbGllcg"}]}';
const LIVE_SOURCE_CONNECTION =
  '{"public_project_reference":"project1.YXRlbGllcg","revision_number":2,"source_kind":"github","source_address":"FlexOr2/atelier-2@main","auth_method":"personal-access-token","project_source_connection_revision_hash":"a8e3ef4bf17dcb0262d2cc5ad2073133437a189a69d437f4b50c072fab31a7bc"}';
const LIVE_SOURCES =
  '{"items":[{"public_source_reference":"source1.MzgwZjI3YTEtNmRlMC01NjNkLTQwYWItYzg1MzBmOWMyNWNj","kind":"github","address":"FlexOr2/atelier-2","scope":"issues","connected_at":null,"revision":2,"auth_method":"personal-access-token"}]}';
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
const sourceReference = "source1.MzgwZjI3YTEtNmRlMC01NjNkLTQwYWItYzg1MzBmOWMyNWNj";
const projectSource: ProjectSourceResource = {
  public_source_reference: sourceReference,
  kind: "github",
  address: "FlexOr2/atelier-2",
  scope: "issues",
  connected_at: null,
  revision: 2,
  auth_method: "personal-access-token"
};

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
    if (
      method === "GET"
      && url.pathname === `/atelier/api/v1/projects/${LIVE_PROJECT_REFERENCE}/sources`
    ) {
      return jsonResponse(LIVE_SOURCES);
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

function modelRegistryRevisionConflict(): CockpitRequestError {
  return new CockpitRequestError("conflict", {
    type: "urn:atelier2:problem:v1:model-registry-revision-conflict",
    title: "Model registry revision conflict",
    status: 409,
    detail: "The stored revision number does not match."
  } as Problem, true);
}

function projectSourceTokenRefused(): CockpitRequestError {
  return new CockpitRequestError("refused", {
    type: "urn:atelier2:problem:v1:project-source-token-refused",
    title: "Project source token refused",
    status: 422,
    detail: "The provider refused this token."
  } as Problem, true);
}

function projectSourceAlreadyConnected(): CockpitRequestError {
  return new CockpitRequestError("duplicate", {
    type: "urn:atelier2:problem:v1:project-source-already-connected",
    title: "Project source already connected",
    status: 409,
    detail: "A source is already connected."
  } as Problem, true);
}

function echoDefaultsPut() {
  return vi.fn(async (
    _reference: string,
    write: { input: { defaults: ProjectModelDefaultsRevision["defaults"]; revision_number: number }; body: string }
  ) => ({
    status: 200,
    value: { ...defaults(write.input.defaults), revision_number: write.input.revision_number }
  }));
}

function echoRegistryPut() {
  return vi.fn(async (
    _providerId: string,
    write: {
      input: {
        revision_number: number;
        entries: Array<{ model_id: string; agent_configuration_revision_hash: string }>;
      };
      body: string;
    }
  ) => ({
    status: 200,
    value: {
      provider_id: "anthropic",
      revision_number: write.input.revision_number,
      model_registry_revision_hash: registryHash,
      entries: write.input.entries.map((entry) => ({
        ...entry,
        source: "operator" as const,
        provider_check: "checked" as const
      }))
    }
  }));
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
        listProjectSources: vi.fn(async () => ({ items: [projectSource] })),
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


const addedModelId = "new-model-id";
const addedConfigurationHash = "1".repeat(64);

function publishedConfiguration(modelId = addedModelId, hash = addedConfigurationHash) {
  return {
    model: modelId,
    auth_profile_revision_hash: profileHash,
    executor_revision: configuration.executor_revision,
    requested_capability: configuration.requested_capability,
    provider_id: configuration.provider_id,
    auth_mode: configuration.auth_mode,
    agent_configuration_revision_hash: hash
  };
}

function operatorEntry(
  modelId: string,
  hash: string,
  providerCheck: ModelRegistryRevision["entries"][number]["provider_check"]
): ModelRegistryRevision["entries"][number] {
  return {
    model_id: modelId,
    agent_configuration_revision_hash: hash,
    source: "operator",
    provider_check: providerCheck
  };
}

async function fillAddSheet(modelId: string): Promise<HTMLElement> {
  await fireEvent.click(await screen.findByRole("button", { name: settingsPageCopy.addModel }));
  const dialog = await screen.findByRole("dialog", { name: settingsPageCopy.addModel });
  const provider = within(dialog).getByRole("combobox", { name: settingsPageCopy.provider });
  await fireEvent.change(provider, { target: { value: profileHash } });
  await fireEvent.input(within(dialog).getByRole("textbox", { name: settingsPageCopy.model }), {
    target: { value: modelId }
  });
  return dialog;
}

async function submitAddSheet(modelId: string): Promise<void> {
  const dialog = await fillAddSheet(modelId);
  const add = within(dialog).getByRole("button", { name: settingsPageCopy.add });
  await waitFor(() => expect((add as HTMLButtonElement).disabled).toBe(false));
  await fireEvent.click(add);
}

describe("Settings owns project sources, models, and defaults", () => {
  it("renders the v8 order, exact registry evidence, and no run counts", async () => {
    openSettings();

    expect((await screen.findByRole("heading", { name: THE_ONE_PROJECT })).isConnected).toBe(true);
    const sources = await screen.findByRole("heading", { name: settingsPageCopy.sourcesTitle });
    const models = screen.getByRole("heading", { name: settingsPageCopy.modelsTitle });
    expect(screen.getByRole("button", { name: settingsPageCopy.addModel }).closest("table")).not.toBeNull();
    const modelDefaults = screen.getByRole("heading", { name: settingsPageCopy.defaultsTitle });
    expect(sources.compareDocumentPosition(models) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
    expect(models.compareDocumentPosition(modelDefaults) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
    expect(screen.queryByText(/Work in this project/)).toBeNull();
    expect(screen.getByText(configuration.model)).toBeTruthy();
    expect(screen.getByText(profile.profile_id)).toBeTruthy();
    expect(screen.getByText(settingsPageCopy.addedByYouChecked)).toBeTruthy();
    expect(screen.queryByText("✓ startable")).toBeNull();
    expect(screen.getAllByRole("combobox").map((item) => item.getAttribute("aria-label"))).toEqual([
      difficultyLabel(3),
      difficultyLabel(2),
      difficultyLabel(1)
    ]);
    expect(screen.queryByText(/Saving|Saved/)).toBeNull();
  });

  it("writes registry membership immediately with frozen exact bytes", async () => {
    const publishAgentConfiguration = vi.fn(async () => ({
      status: 201 as const,
      value: publishedConfiguration()
    }));
    const putModelRegistry = vi.fn(async (_providerId, write) => ({
      status: 200,
      value: { ...registry(write.input.entries), revision_number: write.input.revision_number }
    }));
    const validateModelRegistryEntry = vi.fn(async () => ({
      status: 201,
      value: registry([operatorEntry(addedModelId, addedConfigurationHash, "checked")])
    }));
    openSettings({
      getModelRegistry: vi.fn(async () => registry([])),
      getProjectModelDefaults: vi.fn(async () => defaults([])),
      publishAgentConfiguration,
      putModelRegistry,
      validateModelRegistryEntry
    });

    await submitAddSheet(addedModelId);

    await waitFor(() => expect(publishAgentConfiguration).toHaveBeenCalledTimes(1));
    expect(publishAgentConfiguration).toHaveBeenCalledWith({
      model: addedModelId,
      auth_profile_revision_hash: profileHash,
      executor_revision: configuration.executor_revision,
      requested_capability: configuration.requested_capability
    });
    await waitFor(() => expect(putModelRegistry).toHaveBeenCalledTimes(1));
    const [, write] = putModelRegistry.mock.calls[0] ?? [];
    expect(write.body).toBe(JSON.stringify(write.input));
    expect(write.input).toEqual({
      revision_number: 2,
      entries: [{
        model_id: addedModelId,
        agent_configuration_revision_hash: addedConfigurationHash
      }]
    });
    expect(validateModelRegistryEntry).toHaveBeenCalledWith("anthropic", addedConfigurationHash);
    expect(await screen.findByText(addedModelId)).toBeTruthy();
    expect(await screen.findByText(settingsPageCopy.addedByYouChecked)).toBeTruthy();
    expect(within(screen.getByRole("combobox", { name: difficultyLabel(3) })).getByRole(
      "option",
      { name: new RegExp(addedModelId) }
    )).toBeTruthy();
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

    expect((await screen.findByText(noSuchModel("anthropic"))).isConnected).toBe(true);
    expect(screen.getByRole("button", { name: settingsPageCopy.correctTheId })).toBeTruthy();
    expect(screen.getByRole("button", { name: settingsPageCopy.remove })).toBeTruthy();
    expect(screen.getByText(settingsPageCopy.defaultsUnavailableModels)).toBeTruthy();
    expect(screen.getByText(retainedAccountChoice(configuration.model, profile.profile_id))).toBeTruthy();
    const difficultyThree = screen.getByRole("combobox", { name: difficultyLabel(3) });
    expect(difficultyThree).toHaveProperty("value", "");
    expect(within(difficultyThree).getByRole("option", { name: settingsPageCopy.changeSavedDefault })).toBeTruthy();
    expect(screen.getByRole("combobox", { name: difficultyLabel(2) })).toBeTruthy();
    expect(screen.getByRole("combobox", { name: difficultyLabel(1) })).toBeTruthy();
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

    await fireEvent.change(await screen.findByRole("combobox", { name: difficultyLabel(3) }), {
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

    await fireEvent.change(await screen.findByRole("combobox", { name: difficultyLabel(2) }), {
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

    expect(await screen.findByRole("button", { name: settingsPageCopy.addModel })).toBeTruthy();
    expect(screen.queryByRole("combobox", { name: settingsPageCopy.addModel })).toBeNull();
    expect(screen.queryByText(configuration.model)).toBeNull();
    expect(within(screen.getByRole("combobox", { name: difficultyLabel(3) })).queryByRole(
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
    expect(within(screen.getByRole("combobox", { name: difficultyLabel(2) })).queryByRole(
      "option",
      { name: new RegExp(configuration.model) }
    )).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: settingsPageCopy.check }));

    await waitFor(() => expect(validateModelRegistryEntry).toHaveBeenCalledWith(
      "anthropic",
      configurationHash
    ));
    expect(within(screen.getByRole("combobox", { name: difficultyLabel(2) })).getByRole(
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

    expect(await screen.findByText(providerAccount("anthropic", profile.profile_id))).toBeTruthy();
    expect(screen.getByText(configuration.model)).toBeTruthy();
    expect(screen.getByRole("button", { name: settingsPageCopy.check })).toBeTruthy();
    expect(screen.getByText(settingsPageCopy.defaultsNoCheckedModels)).toBeTruthy();
    expect(screen.queryByRole("combobox", { name: settingsPageCopy.addModel })).toBeNull();
    expect(within(screen.getByRole("combobox", { name: difficultyLabel(3) })).queryByRole(
      "option",
      { name: new RegExp(configuration.model) }
    )).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: settingsPageCopy.check }));

    await waitFor(() => {
      expect(putModelRegistry).toHaveBeenCalledTimes(1);
      expect(validateModelRegistryEntry).toHaveBeenCalledWith("anthropic", configurationHash);
      expect(within(screen.getByRole("combobox", { name: difficultyLabel(3) })).getByRole(
        "option",
        { name: new RegExp(configuration.model) }
      )).toBeTruthy();
    });
  });

  it("continues Check through validation after a retried uncertain publish", async () => {
    const putModelRegistry = vi
      .fn()
      .mockRejectedValueOnce(new Error("uncertain"))
      .mockImplementation(async (_providerId, write) => ({
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

    await fireEvent.click(await screen.findByRole("button", { name: settingsPageCopy.check }));
    const retry = await screen.findByRole("button", { name: settingsPageCopy.retry });
    expect(screen.getByText(settingsPageCopy.writeFailed)).toBeTruthy();
    expect(validateModelRegistryEntry).not.toHaveBeenCalled();
    await waitFor(() => expect((retry as HTMLButtonElement).disabled).toBe(false));

    await fireEvent.click(retry);

    await waitFor(() => {
      expect(putModelRegistry).toHaveBeenCalledTimes(2);
      expect(putModelRegistry.mock.calls[1]).toEqual(putModelRegistry.mock.calls[0]);
      expect(validateModelRegistryEntry).toHaveBeenCalledWith("anthropic", configurationHash);
      expect(screen.queryByRole("button", { name: settingsPageCopy.retry })).toBeNull();
      expect(within(screen.getByRole("combobox", { name: difficultyLabel(3) })).getByRole(
        "option",
        { name: new RegExp(configuration.model) }
      )).toBeTruthy();
    });
  });

  it("names a validation failure after a retried Check publish so the next Retry only validates", async () => {
    const putModelRegistry = vi
      .fn()
      .mockRejectedValueOnce(new Error("uncertain"))
      .mockImplementation(async (_providerId, write) => ({
        status: 201,
        value: {
          ...registry([{ ...registeredEntry, provider_check: "not-checked" as const }]),
          revision_number: write.input.revision_number,
          model_registry_revision_hash: "f".repeat(64)
        }
      }));
    const validateModelRegistryEntry = vi
      .fn()
      .mockRejectedValueOnce(new Error("uncertain"))
      .mockResolvedValueOnce({
        status: 201,
        value: {
          ...registry([{ ...registeredEntry, provider_check: "checked" as const }]),
          revision_number: 2,
          model_registry_revision_hash: "f".repeat(64)
        }
      });
    openSettings({
      getModelRegistry: vi.fn(async () => { throw modelRegistryMissing(); }),
      getProjectModelDefaults: vi.fn(async () => defaults([])),
      putModelRegistry,
      validateModelRegistryEntry
    });

    await fireEvent.click(await screen.findByRole("button", { name: settingsPageCopy.check }));
    const firstRetry = await screen.findByRole("button", { name: settingsPageCopy.retry });
    await waitFor(() => expect((firstRetry as HTMLButtonElement).disabled).toBe(false));
    await fireEvent.click(firstRetry);

    const secondRetry = await screen.findByRole("button", { name: settingsPageCopy.retry });
    await waitFor(() => {
      expect(putModelRegistry).toHaveBeenCalledTimes(2);
      expect(validateModelRegistryEntry).toHaveBeenCalledTimes(1);
      expect((secondRetry as HTMLButtonElement).disabled).toBe(false);
    });

    await fireEvent.click(secondRetry);

    await waitFor(() => {
      expect(putModelRegistry).toHaveBeenCalledTimes(2);
      expect(validateModelRegistryEntry).toHaveBeenCalledTimes(2);
      expect(screen.queryByRole("button", { name: settingsPageCopy.retry })).toBeNull();
      expect(within(screen.getByRole("combobox", { name: difficultyLabel(3) })).getByRole(
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

    expect((await screen.findByText(providerAccount("anthropic", "operator-anthropic-subscription"))).isConnected).toBe(true);
    expect(screen.getByText(providerAccount("xai", "grok-felix"))).toBeTruthy();
    expect(screen.getByText("claude-opus-4-6")).toBeTruthy();
    expect(screen.getByText("operator-anthropic-subscription")).toBeTruthy();
    expect(screen.getByText("grok-4.6")).toBeTruthy();
    expect(screen.getByText("grok-felix")).toBeTruthy();
    expect(screen.getByRole("button", { name: settingsPageCopy.check })).toBeTruthy();
    expect(screen.queryByRole("combobox", { name: settingsPageCopy.addModel })).toBeNull();

    for (const difficulty of [3, 2, 1]) {
      const select = screen.getByRole("combobox", { name: difficultyLabel(difficulty) });
      expect(within(select).queryByRole("option", {
        name: accountChoice("claude-opus-4-6", "operator-anthropic-subscription")
      })).toBeNull();
      expect(within(select).getByRole("option", {
        name: accountChoice("grok-4.6", "grok-felix")
      })).toBeTruthy();
    }

    await fireEvent.change(screen.getByRole("combobox", { name: difficultyLabel(3) }), {
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

    expect(await screen.findByRole("button", { name: settingsPageCopy.addModel })).toBeTruthy();
    expect(screen.getByRole("button", { name: settingsPageCopy.addModel }).closest("table")).toBeNull();
    expect(screen.queryByText(settingsPageCopy.modelsEmpty)).toBeNull();
    expect(screen.queryByText("No models are registered")).toBeNull();
    expect(screen.queryByText("No source connected")).toBeNull();
    expect(screen.queryByRole("combobox", { name: settingsPageCopy.addModel })).toBeNull();
  });

  it("renders the empty settings form without emptiness sentences", async () => {
    openSettings({
      listProjectSources: vi.fn(async () => ({ items: [] })),
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

    expect(await screen.findByRole("heading", { name: settingsPageCopy.sourcesTitle })).toBeTruthy();
    expect(screen.getByRole("button", { name: settingsPageCopy.connectASource })).toBeTruthy();
    expect(screen.getByRole("heading", { name: settingsPageCopy.modelsTitle })).toBeTruthy();
    expect(screen.getByRole("heading", { name: settingsPageCopy.defaultsTitle })).toBeTruthy();
    expect(screen.getByRole("button", { name: settingsPageCopy.addModel })).toBeTruthy();
    expect(screen.getByRole("combobox", { name: difficultyLabel(3) })).toBeTruthy();
    expect(screen.getByRole("combobox", { name: difficultyLabel(2) })).toBeTruthy();
    expect(screen.getByRole("combobox", { name: difficultyLabel(1) })).toBeTruthy();
    expect(screen.queryByText("No source connected")).toBeNull();
    expect(screen.queryByText("No models are registered")).toBeNull();
    expect(screen.queryByText(settingsPageCopy.modelsEmpty)).toBeNull();
  });

  it("shows a source row without a branch or an auth column", async () => {
    openSettings();

    expect(await screen.findByText(`${settingsPageCopy.github} · ${projectSource.address}`)).toBeTruthy();
    expect(screen.getByText(settingsPageCopy.items)).toBeTruthy();
    expect(screen.getByText(settingsPageCopy.connectionTimeNotRecorded, { exact: false })).toBeTruthy();
    const sources = screen.getByRole("heading", { name: settingsPageCopy.sourcesTitle }).closest("section");
    expect(sources?.textContent).toContain(settingsPageCopy.issues);
    expect(sources?.textContent).not.toContain("@");
    expect(sources?.textContent).not.toContain("personal-access-token");
    expect(screen.queryByText(settingsPageCopy.sourceAuthMethod)).toBeNull();
    expect(screen.getByRole("button", { name: settingsPageCopy.disconnect })).toBeTruthy();
    expect(screen.getByRole("button", { name: settingsPageCopy.renewToken })).toBeTruthy();
  });

  it("connects a source through the sheet and shows the row", async () => {
    const created = { ...projectSource, address: "github.com/FlexOr2/docs", revision: 1 };
    const connectProjectSource = vi.fn(async (_reference: string, request: { address: string; token: string }) => {
      expect(request).toEqual({ address: "github.com/FlexOr2/docs", token: "write-only-token" });
      return created;
    });
    openSettings({
      listProjectSources: vi.fn(async () => ({ items: [] })),
      connectProjectSource
    });

    await fireEvent.click(await screen.findByRole("button", { name: settingsPageCopy.connectASource }));
    const dialog = await screen.findByRole("dialog", { name: settingsPageCopy.connectASource });
    await fireEvent.input(within(dialog).getByRole("textbox", { name: settingsPageCopy.where }), {
      target: { value: "github.com/FlexOr2/docs" }
    });
    await fireEvent.input(within(dialog).getByLabelText(settingsPageCopy.token), {
      target: { value: "write-only-token" }
    });
    const connect = within(dialog).getByRole("button", { name: settingsPageCopy.connect });
    await waitFor(() => expect((connect as HTMLButtonElement).disabled).toBe(false));
    await fireEvent.click(connect);

    await waitFor(() => expect(connectProjectSource).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(`${settingsPageCopy.github} · github.com/FlexOr2/docs`)).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: settingsPageCopy.connectASource })).toBeNull();
  });

  it("keeps Connect disabled for Library", async () => {
    openSettings({ listProjectSources: vi.fn(async () => ({ items: [] })) });

    await fireEvent.click(await screen.findByRole("button", { name: settingsPageCopy.connectASource }));
    const dialog = await screen.findByRole("dialog", { name: settingsPageCopy.connectASource });
    await fireEvent.click(within(dialog).getByRole("button", { name: settingsPageCopy.library }));
    await fireEvent.input(within(dialog).getByRole("textbox", { name: settingsPageCopy.where }), {
      target: { value: "github.com/FlexOr2/docs" }
    });
    await fireEvent.input(within(dialog).getByLabelText(settingsPageCopy.token), {
      target: { value: "write-only-token" }
    });
    expect((within(dialog).getByRole("button", { name: settingsPageCopy.connect }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("asks once more before disconnecting and then removes the row", async () => {
    const disconnectProjectSource = vi.fn(async () => undefined);
    openSettings({ disconnectProjectSource });

    await fireEvent.click(await screen.findByRole("button", { name: settingsPageCopy.disconnect }));
    const dialog = await screen.findByRole("dialog", { name: `Disconnect ${projectSource.address}?` });
    expect(within(dialog).getByText(settingsPageCopy.thisConnection)).toBeTruthy();
    expect(dialog.textContent).toContain(THE_ONE_PROJECT);
    expect(dialog.textContent).toContain(settingsPageCopy.theModels);
    expect(within(dialog).getByText(settingsPageCopy.connectStartsNew)).toBeTruthy();
    await fireEvent.click(within(dialog).getByRole("button", { name: settingsPageCopy.disconnect }));

    await waitFor(() => expect(disconnectProjectSource).toHaveBeenCalledWith(projectReference, sourceReference));
    expect(screen.queryByText(`${settingsPageCopy.github} · ${projectSource.address}`)).toBeNull();
    expect(screen.getByRole("button", { name: settingsPageCopy.connectASource })).toBeTruthy();
    expect(screen.getByRole("heading", { name: settingsPageCopy.modelsTitle })).toBeTruthy();
  });

  it("renews the token without losing the row identity or connection time", async () => {
    const rotateProjectSourceToken = vi.fn(async () => ({ ...projectSource, revision: 3 }));
    openSettings({ rotateProjectSourceToken });

    await fireEvent.click(await screen.findByRole("button", { name: settingsPageCopy.renewToken }));
    const dialog = await screen.findByRole("dialog", { name: settingsPageCopy.renewToken });
    await fireEvent.input(within(dialog).getByLabelText(settingsPageCopy.token), {
      target: { value: "next-token" }
    });
    await fireEvent.click(within(dialog).getByRole("button", { name: settingsPageCopy.renewToken }));

    await waitFor(() => expect(rotateProjectSourceToken).toHaveBeenCalledWith(
      projectReference,
      sourceReference,
      { token: "next-token" }
    ));
    expect(await screen.findByText(`${settingsPageCopy.github} · ${projectSource.address}`)).toBeTruthy();
    expect(screen.getByText(settingsPageCopy.connectionTimeNotRecorded, { exact: false })).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: settingsPageCopy.renewToken })).toBeNull();
  });

  it("shows a refused token as a brick error with one next step and keeps the field secret", async () => {
    const connectProjectSource = vi.fn(async () => { throw projectSourceTokenRefused(); });
    openSettings({
      listProjectSources: vi.fn(async () => ({ items: [] })),
      connectProjectSource
    });

    await fireEvent.click(await screen.findByRole("button", { name: settingsPageCopy.connectASource }));
    const dialog = await screen.findByRole("dialog", { name: settingsPageCopy.connectASource });
    await fireEvent.input(within(dialog).getByRole("textbox", { name: settingsPageCopy.where }), {
      target: { value: "github.com/FlexOr2/docs" }
    });
    await fireEvent.input(within(dialog).getByLabelText(settingsPageCopy.token), {
      target: { value: "write-only-token" }
    });
    await fireEvent.click(within(dialog).getByRole("button", { name: settingsPageCopy.connect }));

    expect(await within(dialog).findByText(settingsPageCopy.tokenRefused)).toBeTruthy();
    expect(within(dialog).getByRole("button", { name: settingsPageCopy.renewToken })).toBeTruthy();
    expect((within(dialog).getByLabelText(settingsPageCopy.token) as HTMLInputElement).type).toBe("password");
    expect(dialog.textContent).not.toContain("write-only-token");
  });

  it("shows the live word while a connect is running", async () => {
    let release: (() => void) | undefined;
    const held = new Promise<void>((resolve) => { release = resolve; });
    const connectProjectSource = vi.fn(async () => {
      await held;
      return projectSource;
    });
    openSettings({
      listProjectSources: vi.fn(async () => ({ items: [] })),
      connectProjectSource
    });

    await fireEvent.click(await screen.findByRole("button", { name: settingsPageCopy.connectASource }));
    const dialog = await screen.findByRole("dialog", { name: settingsPageCopy.connectASource });
    await fireEvent.input(within(dialog).getByRole("textbox", { name: settingsPageCopy.where }), {
      target: { value: "FlexOr2/atelier-2" }
    });
    await fireEvent.input(within(dialog).getByLabelText(settingsPageCopy.token), {
      target: { value: "write-only-token" }
    });
    await fireEvent.click(within(dialog).getByRole("button", { name: settingsPageCopy.connect }));

    expect(await within(dialog).findByText(settingsPageCopy.running)).toBeTruthy();
    expect(dialog.getAttribute("aria-busy")).toBe("true");
    expect((within(dialog).getByRole("button", { name: settingsPageCopy.connect }) as HTMLButtonElement).disabled).toBe(true);
    release?.();
    await waitFor(() => expect(screen.queryByRole("dialog", { name: settingsPageCopy.connectASource })).toBeNull());
  });

  it("wears already present on the existing row instead of adding a second", async () => {
    const connectProjectSource = vi.fn(async () => { throw projectSourceAlreadyConnected(); });
    openSettings({ connectProjectSource });

    await fireEvent.click(await screen.findByRole("button", { name: settingsPageCopy.connectASource }));
    const dialog = await screen.findByRole("dialog", { name: settingsPageCopy.connectASource });
    await fireEvent.input(within(dialog).getByRole("textbox", { name: settingsPageCopy.where }), {
      target: { value: "FlexOr2/atelier-2" }
    });
    await fireEvent.input(within(dialog).getByLabelText(settingsPageCopy.token), {
      target: { value: "write-only-token" }
    });
    await fireEvent.click(within(dialog).getByRole("button", { name: settingsPageCopy.connect }));

    await waitFor(() => expect(screen.queryByRole("dialog", { name: settingsPageCopy.connectASource })).toBeNull());
    expect(screen.getByText(settingsPageCopy.alreadyPresent)).toBeTruthy();
    expect(screen.getAllByText(`${settingsPageCopy.github} · ${projectSource.address}`)).toHaveLength(1);
  });

  it("shows a disconnect failure as a brick error with Retry", async () => {
    const disconnectProjectSource = vi.fn(async () => { throw new Error("transport"); });
    openSettings({ disconnectProjectSource });

    await fireEvent.click(await screen.findByRole("button", { name: settingsPageCopy.disconnect }));
    const dialog = await screen.findByRole("dialog", { name: `Disconnect ${projectSource.address}?` });
    await fireEvent.click(within(dialog).getByRole("button", { name: settingsPageCopy.disconnect }));

    expect(await within(dialog).findByText(settingsPageCopy.sourceDisconnectRefused)).toBeTruthy();
    expect(within(dialog).getByRole("button", { name: settingsPageCopy.retry })).toBeTruthy();
  });

  it("re-reads sources when connect is already present and the page is empty", async () => {
    const connectProjectSource = vi.fn(async () => { throw projectSourceAlreadyConnected(); });
    const listProjectSources = vi.fn()
      .mockResolvedValueOnce({ items: [] })
      .mockResolvedValueOnce({ items: [projectSource] });
    openSettings({ listProjectSources, connectProjectSource });

    await fireEvent.click(await screen.findByRole("button", { name: settingsPageCopy.connectASource }));
    const dialog = await screen.findByRole("dialog", { name: settingsPageCopy.connectASource });
    await fireEvent.input(within(dialog).getByRole("textbox", { name: settingsPageCopy.where }), {
      target: { value: "FlexOr2/atelier-2" }
    });
    await fireEvent.input(within(dialog).getByLabelText(settingsPageCopy.token), {
      target: { value: "write-only-token" }
    });
    await fireEvent.click(within(dialog).getByRole("button", { name: settingsPageCopy.connect }));

    expect(await screen.findByText(settingsPageCopy.alreadyPresent)).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: settingsPageCopy.connectASource })).toBeNull();
    expect(screen.getAllByText(`${settingsPageCopy.github} · ${projectSource.address}`)).toHaveLength(1);
    expect(listProjectSources).toHaveBeenCalledTimes(2);
  });

  it("keeps Connect open with Retry when a duplicate re-read is still empty", async () => {
    const connectProjectSource = vi.fn(async () => { throw projectSourceAlreadyConnected(); });
    const listProjectSources = vi.fn(async () => ({ items: [] }));
    openSettings({ listProjectSources, connectProjectSource });

    await fireEvent.click(await screen.findByRole("button", { name: settingsPageCopy.connectASource }));
    const dialog = await screen.findByRole("dialog", { name: settingsPageCopy.connectASource });
    await fireEvent.input(within(dialog).getByRole("textbox", { name: settingsPageCopy.where }), {
      target: { value: "FlexOr2/atelier-2" }
    });
    await fireEvent.input(within(dialog).getByLabelText(settingsPageCopy.token), {
      target: { value: "write-only-token" }
    });
    await fireEvent.click(within(dialog).getByRole("button", { name: settingsPageCopy.connect }));

    expect(await within(dialog).findByText(settingsPageCopy.sourceNotShown)).toBeTruthy();
    const retry = within(dialog).getByRole("button", { name: settingsPageCopy.retry });
    expect(retry).toBeTruthy();
    expect(screen.queryByText(settingsPageCopy.alreadyPresent)).toBeNull();
    expect(listProjectSources).toHaveBeenCalledTimes(2);

    await waitFor(() => expect((retry as HTMLButtonElement).disabled).toBe(false));
    await fireEvent.click(retry);
    await waitFor(() => expect(listProjectSources).toHaveBeenCalledTimes(3));
    expect(within(dialog).getByText(settingsPageCopy.sourceNotShown)).toBeTruthy();
    expect(screen.queryByText(settingsPageCopy.alreadyPresent)).toBeNull();
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

    await fireEvent.change(await screen.findByRole("combobox", { name: difficultyLabel(3) }), {
      target: { value: "" }
    });
    await fireEvent.click(await screen.findByRole("button", { name: settingsPageCopy.retry }));

    await waitFor(() => expect(putProjectModelDefaults).toHaveBeenCalledTimes(2));
    expect(putProjectModelDefaults.mock.calls[1]).toEqual(putProjectModelDefaults.mock.calls[0]);
  });

  it("retries the identical uncertain registry write", async () => {
    const getModelRegistry = vi.fn(async () => registry([registeredEntry]));
    const putProjectModelDefaults = echoDefaultsPut();
    const putModelRegistry = vi
      .fn()
      .mockRejectedValueOnce(new Error("uncertain"))
      .mockImplementationOnce(async (_providerId, write) => ({
        status: 200,
        value: { ...registry(write.input.entries), revision_number: write.input.revision_number }
      }));
    openSettings({ getModelRegistry, putModelRegistry, putProjectModelDefaults });

    await fireEvent.click(await screen.findByRole("button", { name: settingsPageCopy.remove }));
    await fireEvent.click(await screen.findByRole("button", { name: settingsPageCopy.retry }));

    await waitFor(() => expect(putModelRegistry).toHaveBeenCalledTimes(2));
    expect(putModelRegistry.mock.calls[1]).toEqual(putModelRegistry.mock.calls[0]);
    expect(getModelRegistry).toHaveBeenCalledTimes(1);
  });

  it("clears saved defaults that name a removed model before dropping the registry entry", async () => {
    const putProjectModelDefaults = echoDefaultsPut();
    const putModelRegistry = echoRegistryPut();
    openSettings({ putProjectModelDefaults, putModelRegistry });

    await fireEvent.click(await screen.findByRole("button", { name: settingsPageCopy.remove }));

    await waitFor(() => {
      expect(putProjectModelDefaults).toHaveBeenCalledTimes(1);
      expect(putModelRegistry).toHaveBeenCalledTimes(1);
    });
    expect(putProjectModelDefaults.mock.invocationCallOrder[0]).toBeLessThan(
      putModelRegistry.mock.invocationCallOrder[0] ?? 0
    );
    const defaultsWrite = putProjectModelDefaults.mock.calls[0]?.[1];
    if (defaultsWrite === undefined) throw new Error("expected a defaults write");
    expect(defaultsWrite.body).toBe(JSON.stringify(defaultsWrite.input));
    expect(defaultsWrite.input.defaults.some(
      (item) => item.difficulty === 3 && item.agent_configuration_revision_hash === configurationHash
    )).toBe(false);
    const registryWrite = putModelRegistry.mock.calls[0]?.[1];
    if (registryWrite === undefined) throw new Error("expected a registry write");
    expect(registryWrite.input.entries.map(
      (entry) => entry.agent_configuration_revision_hash
    )).not.toContain(configurationHash);
    expect(within(screen.getByRole("combobox", { name: difficultyLabel(3) })).queryByRole(
      "option",
      { name: new RegExp(configuration.model) }
    )).toBeNull();
  });

  it("does not list an added model when the configuration POST fails", async () => {
    const input = {
      model: addedModelId,
      auth_profile_revision_hash: profileHash,
      executor_revision: configuration.executor_revision,
      requested_capability: configuration.requested_capability
    };
    const publishAgentConfiguration = vi.fn().mockRejectedValue(new Error("uncertain"));
    const putModelRegistry = vi.fn();
    openSettings({
      getModelRegistry: vi.fn(async () => { throw modelRegistryMissing(); }),
      getProjectModelDefaults: vi.fn(async () => defaults([])),
      publishAgentConfiguration,
      putModelRegistry
    });

    await submitAddSheet(addedModelId);

    expect(await screen.findByText(settingsPageCopy.writeFailed)).toBeTruthy();
    expect(screen.queryByText(addedModelId)).toBeNull();
    expect(publishAgentConfiguration).toHaveBeenCalledTimes(1);
    expect(publishAgentConfiguration).toHaveBeenCalledWith(input);
    expect(putModelRegistry).not.toHaveBeenCalled();

    await fireEvent.click(await screen.findByRole("button", { name: settingsPageCopy.retry }));

    await waitFor(() => expect(publishAgentConfiguration).toHaveBeenCalledTimes(2));
    expect(publishAgentConfiguration.mock.calls[1]?.[0]).toEqual(input);
    expect(putModelRegistry).not.toHaveBeenCalled();
    expect(screen.queryByText(addedModelId)).toBeNull();
  });

  it("does not list an added model when the registry PUT fails", async () => {
    const publishAgentConfiguration = vi.fn(async () => ({
      status: 201 as const,
      value: publishedConfiguration()
    }));
    const putModelRegistry = vi
      .fn()
      .mockRejectedValueOnce(new Error("uncertain"))
      .mockImplementation(async (_providerId, write) => ({
        status: 200,
        value: {
          ...registry(write.input.entries.map((entry: { model_id: string; agent_configuration_revision_hash: string }) => operatorEntry(
            entry.model_id,
            entry.agent_configuration_revision_hash,
            "not-checked"
          ))),
          revision_number: write.input.revision_number
        }
      }));
    const validateModelRegistryEntry = vi.fn(async () => ({
      status: 201,
      value: registry([operatorEntry(addedModelId, addedConfigurationHash, "checked")])
    }));
    openSettings({
      getModelRegistry: vi.fn(async () => { throw modelRegistryMissing(); }),
      getProjectModelDefaults: vi.fn(async () => defaults([])),
      publishAgentConfiguration,
      putModelRegistry,
      validateModelRegistryEntry
    });

    await submitAddSheet(addedModelId);

    expect(await screen.findByText(settingsPageCopy.writeFailed)).toBeTruthy();
    expect(screen.queryByText(addedModelId)).toBeNull();
    expect(publishAgentConfiguration).toHaveBeenCalledTimes(1);
    expect(putModelRegistry).toHaveBeenCalledTimes(1);
    expect(validateModelRegistryEntry).not.toHaveBeenCalled();

    await fireEvent.click(await screen.findByRole("button", { name: settingsPageCopy.retry }));

    await waitFor(() => expect(putModelRegistry).toHaveBeenCalledTimes(2));
    expect(publishAgentConfiguration).toHaveBeenCalledTimes(1);
    const [, write] = putModelRegistry.mock.calls[1] ?? [];
    expect(write.input.entries).toEqual([{
      model_id: addedModelId,
      agent_configuration_revision_hash: addedConfigurationHash
    }]);
    expect(await screen.findByText(addedModelId)).toBeTruthy();
    expect(validateModelRegistryEntry).toHaveBeenCalledWith("anthropic", addedConfigurationHash);
  });

  it("does not present a checked add when validation fails", async () => {
    const publishAgentConfiguration = vi.fn(async () => ({
      status: 201 as const,
      value: publishedConfiguration()
    }));
    const putModelRegistry = vi.fn(async (_providerId, write) => ({
      status: 200,
      value: {
        ...registry(write.input.entries.map((entry: { model_id: string; agent_configuration_revision_hash: string }) => operatorEntry(
          entry.model_id,
          entry.agent_configuration_revision_hash,
          "not-checked"
        ))),
        revision_number: write.input.revision_number
      }
    }));
    const validateModelRegistryEntry = vi
      .fn()
      .mockRejectedValueOnce(new Error("uncertain"))
      .mockResolvedValueOnce({
        status: 201,
        value: registry([operatorEntry(addedModelId, addedConfigurationHash, "checked")])
      });
    openSettings({
      getModelRegistry: vi.fn(async () => registry([])),
      getProjectModelDefaults: vi.fn(async () => defaults([])),
      publishAgentConfiguration,
      putModelRegistry,
      validateModelRegistryEntry
    });

    await submitAddSheet(addedModelId);

    expect(await screen.findByText(settingsPageCopy.writeFailed)).toBeTruthy();
    expect(screen.queryByText(settingsPageCopy.addedByYouChecked)).toBeNull();
    expect(publishAgentConfiguration).toHaveBeenCalledTimes(1);
    expect(putModelRegistry).toHaveBeenCalledTimes(1);
    expect(validateModelRegistryEntry).toHaveBeenCalledTimes(1);

    await fireEvent.click(await screen.findByRole("button", { name: settingsPageCopy.retry }));

    await waitFor(() => expect(validateModelRegistryEntry).toHaveBeenCalledTimes(2));
    expect(publishAgentConfiguration).toHaveBeenCalledTimes(1);
    expect(putModelRegistry).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(settingsPageCopy.addedByYouChecked)).toBeTruthy();
  });

  it("rebases a conflicting registry remove onto the current revision without a second click", async () => {
    const siblingHash = "2".repeat(64);
    const sibling = {
      model_id: "sibling-model",
      agent_configuration_revision_hash: siblingHash,
      source: "discovered" as const,
      provider_check: "checked" as const
    };
    const currentAfterConflict = {
      provider_id: "anthropic",
      revision_number: 2,
      model_registry_revision_hash: "f".repeat(64),
      entries: [registeredEntry, sibling]
    };
    const getModelRegistry = vi
      .fn()
      .mockResolvedValueOnce(registry([registeredEntry]))
      .mockResolvedValueOnce(currentAfterConflict);
    const putProjectModelDefaults = echoDefaultsPut();
    const putModelRegistry = vi
      .fn()
      .mockRejectedValueOnce(modelRegistryRevisionConflict())
      .mockImplementation(async (_providerId, write) => ({
        status: 200,
        value: {
          provider_id: "anthropic",
          revision_number: write.input.revision_number,
          model_registry_revision_hash: "e".repeat(64),
          entries: write.input.entries.map((entry: { model_id: string; agent_configuration_revision_hash: string }) => ({
            ...entry,
            source: "discovered" as const,
            provider_check: "checked" as const
          }))
        }
      }));
    openSettings({ getModelRegistry, putModelRegistry, putProjectModelDefaults });

    await fireEvent.click(await screen.findByRole("button", { name: settingsPageCopy.remove }));

    await waitFor(() => expect(putModelRegistry).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("button", { name: settingsPageCopy.retry })).toBeNull();
    const [, write] = putModelRegistry.mock.calls[1] ?? [];
    expect(write.input.revision_number).toBe(3);
    expect(write.input.entries).toEqual([{
      model_id: sibling.model_id,
      agent_configuration_revision_hash: siblingHash
    }]);
    expect(await screen.findByText("sibling-model")).toBeTruthy();
    expect(screen.queryByText(configuration.model)).toBeNull();
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
    expect((await screen.findByRole("button", { name: settingsPageCopy.addModel })).isConnected).toBe(true);
    expect(screen.queryByText(settingsPageCopy.modelsEmpty)).toBeNull();
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

    await fireEvent.click(await screen.findByRole("button", { name: retryLabel(settingsPageCopy.label) }));

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


  it("names an already present model instead of publishing a duplicate", async () => {
    const publishAgentConfiguration = vi.fn();
    openSettings({ publishAgentConfiguration });

    await submitAddSheet(configuration.model);

    expect(publishAgentConfiguration).not.toHaveBeenCalled();
    expect(await screen.findByText(settingsPageCopy.alreadyPresent)).toBeTruthy();
    expect(screen.getAllByText(configuration.model)).toHaveLength(1);
    expect(screen.queryByRole("dialog", { name: settingsPageCopy.addModel })).toBeNull();
  });

  it("shows no such model and Correct the id after an unknown provider check", async () => {
    const publishAgentConfiguration = vi.fn(async () => ({
      status: 201 as const,
      value: publishedConfiguration()
    }));
    const putModelRegistry = vi.fn(async (_providerId, write) => ({
      status: 200,
      value: { ...registry(write.input.entries), revision_number: write.input.revision_number }
    }));
    const validateModelRegistryEntry = vi.fn(async () => ({
      status: 201,
      value: registry([operatorEntry(addedModelId, addedConfigurationHash, "unknown-at-provider")])
    }));
    openSettings({
      getModelRegistry: vi.fn(async () => registry([])),
      getProjectModelDefaults: vi.fn(async () => defaults([])),
      publishAgentConfiguration,
      putModelRegistry,
      validateModelRegistryEntry
    });

    await submitAddSheet(addedModelId);

    expect(await screen.findByText(noSuchModel("anthropic"))).toBeTruthy();
    expect(screen.getByRole("button", { name: settingsPageCopy.correctTheId })).toBeTruthy();
    expect(screen.getByRole("button", { name: settingsPageCopy.remove })).toBeTruthy();
    expect(screen.queryByRole("button", { name: settingsPageCopy.check })).toBeNull();
  });

  it("keeps Remove enabled while Checking a just-added model", async () => {
    let release!: (value: {
      status: number;
      value: ModelRegistryRevision;
    }) => void;
    const publishAgentConfiguration = vi.fn(async () => ({
      status: 201 as const,
      value: publishedConfiguration()
    }));
    const putModelRegistry = vi.fn(async (_providerId, write) => ({
      status: 200,
      value: {
        ...registry(write.input.entries.map((entry: { model_id: string; agent_configuration_revision_hash: string }) => operatorEntry(
          entry.model_id,
          entry.agent_configuration_revision_hash,
          "not-checked"
        ))),
        revision_number: write.input.revision_number
      }
    }));
    const validateModelRegistryEntry = vi.fn(() => new Promise<{
      status: number;
      value: ModelRegistryRevision;
    }>((resolve) => {
      release = resolve;
    }));
    openSettings({
      getModelRegistry: vi.fn(async () => registry([])),
      getProjectModelDefaults: vi.fn(async () => defaults([])),
      publishAgentConfiguration,
      putModelRegistry,
      validateModelRegistryEntry
    });

    await submitAddSheet(addedModelId);

    expect(await screen.findByText(settingsPageCopy.checking)).toBeTruthy();
    const remove = screen.getByRole("button", { name: settingsPageCopy.remove });
    expect((remove as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByRole("button", { name: settingsPageCopy.check })).toBeNull();

    release({
      status: 201,
      value: registry([operatorEntry(addedModelId, addedConfigurationHash, "checked")])
    });

    await waitFor(() => {
      expect(screen.queryByText(settingsPageCopy.checking)).toBeNull();
      expect(screen.getByText(settingsPageCopy.addedByYouChecked)).toBeTruthy();
    });
  });

  it("offers the added model in difficulty 3 after a checked validate", async () => {
    const publishAgentConfiguration = vi.fn(async () => ({
      status: 201 as const,
      value: publishedConfiguration()
    }));
    const putModelRegistry = vi.fn(async (_providerId, write) => ({
      status: 200,
      value: {
        ...registry(write.input.entries.map((entry: { model_id: string; agent_configuration_revision_hash: string }) => operatorEntry(
          entry.model_id,
          entry.agent_configuration_revision_hash,
          "not-checked"
        ))),
        revision_number: write.input.revision_number
      }
    }));
    const validateModelRegistryEntry = vi.fn(async () => ({
      status: 201,
      value: registry([operatorEntry(addedModelId, addedConfigurationHash, "checked")])
    }));
    openSettings({
      getModelRegistry: vi.fn(async () => registry([])),
      getProjectModelDefaults: vi.fn(async () => defaults([])),
      publishAgentConfiguration,
      putModelRegistry,
      validateModelRegistryEntry
    });

    await submitAddSheet(addedModelId);

    await waitFor(() => {
      expect(within(screen.getByRole("combobox", { name: difficultyLabel(3) })).getByRole(
        "option",
        { name: new RegExp(addedModelId) }
      )).toBeTruthy();
    });
  });

  it("does not resurrect a model removed while Checking", async () => {
    let release!: (value: {
      status: number;
      value: ModelRegistryRevision;
    }) => void;
    const publishAgentConfiguration = vi.fn(async () => ({
      status: 201 as const,
      value: publishedConfiguration()
    }));
    const putModelRegistry = vi.fn(async (_providerId, write) => ({
      status: 200,
      value: {
        ...registry(write.input.entries.map((entry: { model_id: string; agent_configuration_revision_hash: string }) => operatorEntry(
          entry.model_id,
          entry.agent_configuration_revision_hash,
          "not-checked"
        ))),
        revision_number: write.input.revision_number
      }
    }));
    const validateModelRegistryEntry = vi.fn(() => new Promise<{
      status: number;
      value: ModelRegistryRevision;
    }>((resolve) => {
      release = resolve;
    }));
    openSettings({
      getModelRegistry: vi.fn(async () => registry([])),
      getProjectModelDefaults: vi.fn(async () => defaults([])),
      publishAgentConfiguration,
      putModelRegistry,
      validateModelRegistryEntry
    });

    await submitAddSheet(addedModelId);
    expect(await screen.findByText(settingsPageCopy.checking)).toBeTruthy();
    const remove = screen.getByRole("button", { name: settingsPageCopy.remove });
    expect((remove as HTMLButtonElement).disabled).toBe(false);

    await fireEvent.click(remove);
    release({
      status: 201,
      value: registry([operatorEntry(addedModelId, addedConfigurationHash, "checked")])
    });

    await waitFor(() => expect(screen.queryByText(addedModelId)).toBeNull());
    expect(screen.queryByText(settingsPageCopy.writeFailed)).toBeNull();
    const retry = screen.queryByRole("button", { name: settingsPageCopy.retry });
    if (retry !== null) {
      await fireEvent.click(retry);
      await waitFor(() => expect(screen.queryByText(addedModelId)).toBeNull());
    }
    await waitFor(() => expect(putModelRegistry).toHaveBeenCalled());
    const lastWrite = putModelRegistry.mock.calls.at(-1)?.[1];
    expect(lastWrite?.input.entries.map(
      (entry: { agent_configuration_revision_hash: string }) => entry.agent_configuration_revision_hash
    )).not.toContain(addedConfigurationHash);
  });

  it("retries the same configuration POST after an uncertain add", async () => {
    const input = {
      model: addedModelId,
      auth_profile_revision_hash: profileHash,
      executor_revision: configuration.executor_revision,
      requested_capability: configuration.requested_capability
    };
    const publishAgentConfiguration = vi
      .fn()
      .mockRejectedValueOnce(new Error("uncertain"))
      .mockResolvedValueOnce({
        status: 201 as const,
        value: publishedConfiguration()
      });
    const putModelRegistry = vi.fn(async (_providerId, write) => ({
      status: 200,
      value: {
        ...registry(write.input.entries.map((entry: { model_id: string; agent_configuration_revision_hash: string }) => operatorEntry(
          entry.model_id,
          entry.agent_configuration_revision_hash,
          "not-checked"
        ))),
        revision_number: write.input.revision_number
      }
    }));
    const validateModelRegistryEntry = vi.fn(async () => ({
      status: 201,
      value: registry([operatorEntry(addedModelId, addedConfigurationHash, "checked")])
    }));
    openSettings({
      getModelRegistry: vi.fn(async () => registry([])),
      getProjectModelDefaults: vi.fn(async () => defaults([])),
      publishAgentConfiguration,
      putModelRegistry,
      validateModelRegistryEntry
    });

    await submitAddSheet(addedModelId);

    expect(await screen.findByText(settingsPageCopy.writeFailed)).toBeTruthy();
    expect(publishAgentConfiguration).toHaveBeenCalledTimes(1);
    expect(publishAgentConfiguration).toHaveBeenCalledWith(input);
    expect((screen.getByRole("button", { name: settingsPageCopy.addModel }) as HTMLButtonElement).disabled).toBe(true);
    expect((within(screen.getByRole("dialog", { name: settingsPageCopy.addModel })).getByRole(
      "button",
      { name: settingsPageCopy.add }
    ) as HTMLButtonElement).disabled).toBe(true);

    await fireEvent.click(await screen.findByRole("button", { name: settingsPageCopy.retry }));

    await waitFor(() => expect(publishAgentConfiguration).toHaveBeenCalledTimes(2));
    expect(publishAgentConfiguration.mock.calls[1]?.[0]).toEqual(input);
    expect(publishAgentConfiguration.mock.calls[1]).toEqual(publishAgentConfiguration.mock.calls[0]);
    await waitFor(() => expect(putModelRegistry).toHaveBeenCalledTimes(1));
    expect(validateModelRegistryEntry).toHaveBeenCalledWith("anthropic", addedConfigurationHash);
    expect(await screen.findByText(addedModelId)).toBeTruthy();
    expect(await screen.findByText(settingsPageCopy.addedByYouChecked)).toBeTruthy();
    expect(within(screen.getByRole("combobox", { name: difficultyLabel(3) })).getByRole(
      "option",
      { name: new RegExp(addedModelId) }
    )).toBeTruthy();
  });

  it("removes an added model from the table and difficulty options", async () => {
    const publishAgentConfiguration = vi.fn(async () => ({
      status: 201 as const,
      value: publishedConfiguration()
    }));
    const putModelRegistry = vi.fn(async (_providerId, write) => ({
      status: 200,
      value: {
        ...registry(write.input.entries.map((entry: { model_id: string; agent_configuration_revision_hash: string }) => operatorEntry(
          entry.model_id,
          entry.agent_configuration_revision_hash,
          "checked"
        ))),
        revision_number: write.input.revision_number
      }
    }));
    const validateModelRegistryEntry = vi.fn(async () => ({
      status: 201,
      value: registry([operatorEntry(addedModelId, addedConfigurationHash, "checked")])
    }));
    openSettings({
      getModelRegistry: vi.fn(async () => registry([])),
      getProjectModelDefaults: vi.fn(async () => defaults([])),
      publishAgentConfiguration,
      putModelRegistry,
      validateModelRegistryEntry
    });

    await submitAddSheet(addedModelId);
    expect(await screen.findByText(addedModelId)).toBeTruthy();
    await fireEvent.click(screen.getByRole("button", { name: settingsPageCopy.remove }));

    await waitFor(() => expect(screen.queryByText(addedModelId)).toBeNull());
    expect(within(screen.getByRole("combobox", { name: difficultyLabel(3) })).queryByRole(
      "option",
      { name: new RegExp(addedModelId) }
    )).toBeNull();
  });

  it("carries no local trail back from the rail destination", async () => {
    openSettings();
    await screen.findByRole("heading", { name: THE_ONE_PROJECT });
    expect(screen.queryByRole("navigation", { name: backLinkCopy.whereYouAre })).toBeNull();
  });
});
