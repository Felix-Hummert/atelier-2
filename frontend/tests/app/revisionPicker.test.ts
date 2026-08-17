import { cleanup, render, screen } from "@testing-library/svelte";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import { workflowRevisionSummarySchema, type CockpitApi } from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { cockpitApiStub } from "../support/cockpitApi";

/**
 * The rows this picker renders are decoded, never invented.
 *
 * A mocked listing is what let an enriched wire break the real page once
 * already: every frontend test handed the page an object the decoder had never
 * seen. So each row here goes through the real `workflowRevisionSummarySchema`
 * before the page receives it, and the field set is checked against the frozen
 * document — a row this helper accepts is a row the server can actually send.
 */
const servedDocument = JSON.parse(
  readFileSync(resolve(process.cwd(), "..", "tests", "api", "openapi_frozen.json"), "utf8")
) as { components: { schemas: Record<string, { properties?: Record<string, unknown> }> } };

function decodedRow(row: Record<string, unknown>) {
  const served = servedDocument.components.schemas.WorkflowRevisionSummaryResourceV2;
  expect(Object.keys(row).sort()).toEqual(Object.keys(served?.properties ?? {}).sort());
  return workflowRevisionSummarySchema.parse(row);
}

const namedHash = "a".repeat(64);
const unnamedHash = "b".repeat(64);

const namedRevision = () =>
  decodedRow({
    revision_hash: namedHash,
    format_version: 3,
    executable: false,
    not_executable_reason: "agent forms nothing binds yet: outputs",
    name: "Implement a candidate, then review it for defects",
    description: "Builds the candidate, then reviews it for defects."
  });

const unnamedRevision = () =>
  decodedRow({
    revision_hash: unnamedHash,
    format_version: 2,
    executable: true,
    not_executable_reason: null,
    name: null,
    description: null
  });

function api(items: ReturnType<typeof decodedRow>[]): CockpitApi {
  return cockpitApiStub({
    listWorkflowRevisions: vi.fn(async () => ({
      items,
      next_after_revision_hash: null
    }))
  });
}

function renderPicker(items: ReturnType<typeof decodedRow>[]): void {
  render(App, {
    props: {
      cockpitApi: api(items),
      mutationJournal: new MutationJournal(sessionStorage)
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

describe("the saved-workflow picker", () => {
  it("offers a named workflow by its name and keeps the exact hash under details", async () => {
    renderPicker([namedRevision()]);

    const option = await screen.findByRole("radio", {
      name: /Implement a candidate, then review it for defects/
    });

    expect(option).toBeTruthy();
    expect(option.closest("label")?.textContent).not.toContain(namedHash);
    expect(screen.getByText("Builds the candidate, then reviews it for defects.")).toBeTruthy();
    const details = screen.getByText("Details").closest("details");
    expect(details?.textContent).toContain(namedHash);
    expect(details?.open).toBe(false);
  });

  it("proves(a-revision-no-run-can-start-says-so-before-the-operator-tries): says a revision cannot be started, and why, before it is chosen", async () => {
    renderPicker([namedRevision()]);

    const option = await screen.findByRole("radio", {
      name: /Implement a candidate, then review it for defects/
    });

    expect(option).toHaveProperty("disabled", true);
    // Extension, named: this asserted the refusal said "format 3", which blamed
    // the version. The server names the authored form that is waiting now, and
    // the sentence asks for the reason -- so the stronger half is pinned here.
    expect(screen.getByText(/cannot be started/i).textContent).toContain(
      "agent forms nothing binds yet: outputs"
    );
  });

  it("keeps the hash as the label of a revision whose format declares no name", async () => {
    renderPicker([unnamedRevision()]);

    const option = await screen.findByRole("radio", { name: /unnamed/i });

    expect(option).toHaveProperty("disabled", false);
    expect(screen.getByText(unnamedHash)).toBeTruthy();
    expect(screen.queryByText("Details")).toBeNull();
  });

  it("leaves a startable revision selectable and says nothing about starting it", async () => {
    renderPicker([unnamedRevision()]);

    await screen.findByRole("radio", { name: /unnamed/i });

    expect(screen.queryByText(/cannot be started/i)).toBeNull();
  });
});

/** A picker driven by pages, exactly as the route serves them. */
function pagedApi(pages: ReturnType<typeof decodedRow>[][]): CockpitApi {
  return cockpitApiStub({
    listWorkflowRevisions: vi.fn(async (after?: string) => {
      const index = after === undefined ? 0 : pages.findIndex((page) => page.at(-1)?.revision_hash === after) + 1;
      const items = pages[index] ?? [];
      const last = index + 1 < pages.length ? items.at(-1)?.revision_hash ?? null : null;
      return { items, next_after_revision_hash: last };
    })
  });
}

describe("the picker reads past its first page", () => {
  it("proves(the-picker-offers-every-saved-workflow-not-only-its-first-page): offers a named workflow that only exists on a later page", async () => {
    const cockpitApi = pagedApi([[unnamedRevision()], [namedRevision()]]);
    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    expect(
      (await screen.findByRole("radio", {
        name: /Implement a candidate, then review it for defects/
      })).isConnected
    ).toBe(true);
    expect(vi.mocked(cockpitApi.listWorkflowRevisions).mock.calls.map(([after]) => after)).toEqual([
      undefined,
      unnamedHash
    ]);
  });

  it("names each disclosure by the workflow it belongs to", async () => {
    const second = decodedRow({
      revision_hash: "c".repeat(64),
      format_version: 3,
      executable: false,
      not_executable_reason: "agent forms nothing binds yet: outputs",
      name: "Sweep the suite and file what broke",
      description: null
    });
    renderPicker([namedRevision(), second]);
    await screen.findByRole("radio", { name: /Implement a candidate/ });

    const names = screen
      .getAllByText("Details")
      .map((summary) => summary.getAttribute("aria-label"));

    expect(names).toEqual([
      "Details for Implement a candidate, then review it for defects",
      "Details for Sweep the suite and file what broke"
    ]);
    expect(new Set(names).size).toBe(names.length);
  });
});
