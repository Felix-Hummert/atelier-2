import { cleanup, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ProjectSourceResource } from "../../src/api/client";
import { settingsPageCopy } from "../../src/lib/settingsPageCopy";
import SettingsPage from "../../src/pages/SettingsPage.svelte";
import { cockpitApiStub } from "../support/cockpitApi";

const projectReference = "project1.dGVzdA";
const source: ProjectSourceResource = {
  public_source_reference: "source1.MzgwZjI3YTEtNmRlMC01NjNkLTQwYWItYzg1MzBmOWMyNWNj",
  kind: "github",
  address: "FlexOr2/atelier-2",
  scope: "issues",
  connected_at: null,
  revision: 2,
  auth_method: "personal-access-token"
};

afterEach(() => {
  cleanup();
});

describe("Settings page copy", () => {
  it("proves(settings-copy-is-one-language): Settings copy is English, the source row renews in English, and the source line never shows an access secret", async () => {
    const german = /erneuern|schon vorhanden|[ÄÖÜäöüß]/;
    for (const [key, value] of Object.entries(settingsPageCopy)) {
      expect(value, key).not.toMatch(german);
    }

    render(SettingsPage, {
      props: {
        cockpitApi: cockpitApiStub({
          listProjects: vi.fn(async () => ({
            items: [{ public_project_reference: projectReference }]
          })),
          listProjectSources: vi.fn(async () => ({ items: [source] })),
          getProjectModelDefaults: vi.fn(async () => ({
            project_id: "atelier",
            public_project_reference: projectReference,
            revision_number: 1,
            project_model_defaults_revision_hash: "d".repeat(64),
            defaults: []
          }))
        })
      }
    });

    const address = await screen.findByText(source.address, { exact: false });
    const row = address.closest("li");
    expect(row).toBeInstanceOf(HTMLLIElement);
    expect(row?.textContent).not.toMatch(german);
    expect(row?.textContent).not.toContain("personal-access-token");
    expect(screen.getByRole("button", { name: settingsPageCopy.renewToken }).closest("li")).toBe(
      row
    );
    expect(settingsPageCopy.renewToken).toBe("Renew token");
    expect(settingsPageCopy.alreadyPresent).toBe("already present");
  });
});
