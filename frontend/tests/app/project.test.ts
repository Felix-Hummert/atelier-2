import { cleanup, render, screen, within } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { wrapDisplayCopy } from "../../src/lib/displayCopy";
import { settingsPageCopy } from "../../src/lib/settingsPageCopy";
import { cockpitApiStub } from "../support/cockpitApi";

const projectReference = "project1.dGVzdA";

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
});

function openSettings(overrides = {}) {
  window.history.replaceState(null, "", "/atelier/settings");
  return render(App, {
    props: {
      cockpitApi: cockpitApiStub({
        listProjects: vi.fn(async () => ({ items: [{ public_project_reference: projectReference }] })),
        getProjectSourceConnection: vi.fn(async () => ({
          public_project_reference: projectReference,
          revision_number: 2,
          source_kind: "github",
          source_address: "FlexOr2/atelier-2",
          auth_method: "personal-access-token" as const,
          project_source_connection_revision_hash: "a".repeat(64)
        })),
        listAgentConfigurationRevisions: vi.fn(async () => ({
          items: [{
            model: "gpt-5.6-sol",
            auth_profile_revision_hash: "b".repeat(64),
            executor_revision: "codex/v1",
            provider_id: "openai",
            auth_mode: "subscription" as const,
            requested_capability: "headless" as const,
            agent_configuration_revision_hash: "c".repeat(64),
            startable: true,
            not_startable_reason: null
          }],
          next_after_revision_hash: null
        })),
        ...overrides
      }),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
}

describe("Settings", () => {
  it("reads the connected source without exposing its credential reference", async () => {
    openSettings();

    const sources = await screen.findByRole("region", { name: "Sources" });
    await within(sources).findByText("github");
    expect(within(sources).getByText("github").isConnected).toBe(true);
    expect(within(sources).getByText("FlexOr2/atelier-2").isConnected).toBe(true);
    expect(within(sources).getByText("personal-access-token").isConnected).toBe(true);
    expect(within(sources).getByText("2").isConnected).toBe(true);
    expect(within(sources).queryByText(/credential directory/i)).toBeNull();
  });

  it("lists published configurations and names discovery as a follow-up", async () => {
    openSettings();

    const models = await screen.findByRole("region", { name: "Models" });
    await within(models).findByText("gpt-5.6-sol");
    expect(within(models).getByText("gpt-5.6-sol").isConnected).toBe(true);
    expect(within(models).getByText("openai").isConnected).toBe(true);
    expect(within(models).getByText("codex/v1").isConnected).toBe(true);
    expect(within(models).getByText(settingsPageCopy.discovery).isConnected).toBe(true);
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
    expect(screen.queryByText("Who does the work")).toBeNull();
  });

  it("renders Settings copy through the pseudo-locale transform", async () => {
    window.history.replaceState(null, "", "/atelier/settings?pseudo-locale=1");
    render(App, {
      props: {
        cockpitApi: cockpitApiStub({
          listProjects: vi.fn(async () => ({ items: [{ public_project_reference: projectReference }] })),
          getProjectSourceConnection: vi.fn(async () => ({
            public_project_reference: projectReference,
            revision_number: 2,
            source_kind: "github",
            source_address: "FlexOr2/atelier-2",
            auth_method: "personal-access-token" as const,
            project_source_connection_revision_hash: "a".repeat(64)
          })),
          listAgentConfigurationRevisions: vi.fn(async () => ({ items: [], next_after_revision_hash: null }))
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    const sources = await screen.findByRole("region", { name: wrapDisplayCopy(settingsPageCopy.sourcesTitle) });
    expect(
      (await within(sources).findByText(wrapDisplayCopy(settingsPageCopy.sourceAddress))).isConnected
    ).toBe(true);
    const models = screen.getByRole("region", {
      name: wrapDisplayCopy(settingsPageCopy.modelsTitle)
    });
    expect(
      (await within(models).findByText(wrapDisplayCopy(settingsPageCopy.modelsEmpty))).isConnected
    ).toBe(true);
  });
});
