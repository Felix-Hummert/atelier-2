import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  CockpitRequestError,
  workflowRevisionSummarySchema,
  type CockpitApi
} from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { cockpitApiStub } from "../support/cockpitApi";
import { utf8Base64 } from "../support/exactBytes";

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
    not_executable_reason:
      "agent-output-shape-unavailable: 0 outputs on node 'implement', and an agent node completes with the one value its own schema judges",
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

/**
 * The graph the detail route already publishes for the named V3 row.
 *
 * Roles and node count live here, not on the listing. The document bytes are a
 * trap: Details must repeat these fields and must not parse that payload.
 */
function namedGraph() {
  return {
    format_version: 3 as const,
    executable: false as const,
    not_executable_reason:
      "agent-output-shape-unavailable: 0 outputs on node 'implement', and an agent node completes with the one value its own schema judges",
    node_count: 2,
    agent_roles: ["builder", "reviewer"],
    orders: [],
    node_previews: [
      {
        id: "implement",
        kind: "agent" as const,
        role: "builder",
        instruction_start: "Implement every acceptance sentence of the bound story.",
        depends_on: []
      },
      {
        id: "review",
        kind: "agent" as const,
        role: "reviewer",
        instruction_start: "Name every defect with the sentence it violates.",
        depends_on: ["implement"]
      }
    ],
    name: "Implement a candidate, then review it for defects",
    description: "Builds the candidate, then reviews it for defects."
  };
}

function namedDetail() {
  return {
    revision_hash: namedHash,
    document_base64: utf8Base64("job: NEVER_PARSE_THIS_INSTRUCTION\n"),
    graph: namedGraph()
  };
}

function api(
  items: ReturnType<typeof decodedRow>[],
  overrides: Partial<CockpitApi> = {}
): CockpitApi {
  return cockpitApiStub({
    listWorkflowRevisions: vi.fn(async () => ({
      items,
      next_after_revision_hash: null
    })),
    ...overrides
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

  it("opening Details for a named V3 revision shows the published roles and node count, not only the hash", async () => {
    const graph = namedGraph();
    const cockpitApi = api([namedRevision()], {
      getWorkflowRevision: vi.fn(async () => namedDetail())
    });
    render(App, {
      props: {
        cockpitApi,
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("radio", {
      name: /Implement a candidate, then review it for defects/
    });

    await fireEvent.click(screen.getByText("Details"));

    const details = screen.getByText("Details").closest("details");
    await waitFor(() => {
      expect(details?.textContent).toContain("builder");
      expect(details?.textContent).toContain("reviewer");
    });
    expect(details?.textContent).toMatch(new RegExp(`${graph.node_count}\\s*nodes`, "i"));
    expect(details?.textContent).toMatch(/format\s*3/i);
    expect(details?.textContent).toContain(
      "Cannot be started: This workflow declares no output on node 'implement'. Add one outputs: entry there and publish again."
    );
    expect(details?.textContent).not.toContain("agent-output-shape-unavailable");
    expect(details?.textContent).toContain(namedHash);
    expect(details?.textContent).not.toBe(namedHash);
    expect(details?.textContent).not.toContain("NEVER_PARSE_THIS_INSTRUCTION");
    expect(vi.mocked(cockpitApi.getWorkflowRevision).mock.calls.map(([hash]) => hash)).toEqual([
      namedHash
    ]);
  });

  it("opening Details shows each published node with its role and instruction start", async () => {
    const graph = namedGraph();
    const cockpitApi = api([namedRevision()], {
      getWorkflowRevision: vi.fn(async () => namedDetail())
    });
    render(App, {
      props: {
        cockpitApi,
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("radio", {
      name: /Implement a candidate, then review it for defects/
    });

    await fireEvent.click(screen.getByText("Details"));

    const details = screen.getByText("Details").closest("details");
    const implementStart = graph.node_previews[0]?.instruction_start ?? "";
    const reviewStart = graph.node_previews[1]?.instruction_start ?? "";
    await waitFor(() => {
      expect(details?.textContent).toContain("builder");
      expect(details?.textContent).toContain(implementStart);
    });
    expect(details?.textContent).toContain("reviewer");
    expect(details?.textContent).toContain(reviewStart);
    expect(details?.querySelectorAll("[data-node-id]")).toHaveLength(graph.node_previews.length);
    expect(details?.textContent).not.toContain("NEVER_PARSE_THIS_INSTRUCTION");
  });

  it("a node without a role or instruction is shown as itself, not filled in", async () => {
    const graph = {
      ...namedGraph(),
      node_count: 1,
      agent_roles: [] as string[],
      orders: [],
      node_previews: [
        {
          id: "approve",
          kind: "wait" as const,
          role: null,
          instruction_start: null,
          depends_on: []
        }
      ]
    };
    const cockpitApi = api(
      [
        decodedRow({
          revision_hash: namedHash,
          format_version: 3,
          executable: false,
          not_executable_reason:
      "agent-output-shape-unavailable: 0 outputs on node 'implement', and an agent node completes with the one value its own schema judges",
          name: "Implement a candidate, then review it for defects",
          description: "Builds the candidate, then reviews it for defects."
        })
      ],
      {
        getWorkflowRevision: vi.fn(async () => ({
          revision_hash: namedHash,
          document_base64: utf8Base64("prompt: NEVER_PARSE_THIS_PROMPT\n"),
          graph
        }))
      }
    );
    render(App, {
      props: {
        cockpitApi,
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    await screen.findByRole("radio", {
      name: /Implement a candidate, then review it for defects/
    });

    await fireEvent.click(screen.getByText("Details"));

    const details = screen.getByText("Details").closest("details");
    await waitFor(() => {
      expect(details?.querySelectorAll("[data-node-id]")).toHaveLength(1);
    });
    expect(details?.textContent).toMatch(/wait/i);
    expect(details?.querySelector(".node-instruction")).toBeNull();
    expect(details?.textContent).not.toContain("NEVER_PARSE_THIS_PROMPT");
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
    // Details now repeats the same published reason, so the row is no longer
    // the only place the words appear.
    for (const refusal of screen.getAllByText(/cannot be started/i)) {
      expect(refusal.textContent).toContain("Add one outputs: entry");
      expect(refusal.textContent).not.toContain("agent-output-shape-unavailable");
    }
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
      not_executable_reason:
      "agent-output-shape-unavailable: 0 outputs on node 'implement', and an agent node completes with the one value its own schema judges",
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

describe("the picker groups revisions that share a published name", () => {
  const lineageName = "drei-saetze-review-sehend";
  const olderHash = "c".repeat(64);
  const newestHash = "d".repeat(64);

  function olderRevision() {
    return decodedRow({
      revision_hash: olderHash,
      format_version: 3,
      executable: true,
      not_executable_reason: null,
      name: lineageName,
      description: "The first admitted member."
    });
  }

  function newestRevision() {
    return decodedRow({
      revision_hash: newestHash,
      format_version: 3,
      executable: false,
      not_executable_reason:
      "agent-output-shape-unavailable: 0 outputs on node 'implement', and an agent node completes with the one value its own schema judges",
      name: lineageName,
      description: "The catalog head."
    });
  }

  function lineageApi(overrides: Partial<CockpitApi> = {}): CockpitApi {
    return api([olderRevision(), newestRevision()], {
      getRevisionByName: vi.fn(async () => ({
        display_name: lineageName,
        lineage_id: "e".repeat(64),
        revision_hash: newestHash,
        revision_number: 2
      })),
      ...overrides
    });
  }

  it("offers two revisions of one lineage as one row, defaults to the newest, and switching revision changes startability", async () => {
    render(App, {
      props: {
        cockpitApi: lineageApi(),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    const option = await screen.findByRole("radio", { name: new RegExp(lineageName) });

    expect(screen.getAllByRole("radio", { name: new RegExp(lineageName) })).toHaveLength(1);
    expect(option).toHaveProperty("disabled", true);
    const article = screen.getByRole("article", { name: lineageName });
    const row = within(article);
    const choice = option.closest("label");
    expect(choice?.textContent).toContain("The catalog head.");
    expect(choice?.textContent).toContain(
      "Cannot be started: This workflow declares no output on node 'implement'. Add one outputs: entry there and publish again."
    );
    expect(choice?.textContent).not.toContain("agent-output-shape-unavailable");
    expect(choice?.textContent).not.toContain("The first admitted member.");

    await fireEvent.change(row.getByLabelText(`Revision of ${lineageName}`), {
      target: { value: olderHash }
    });

    expect(screen.getByRole("radio", { name: new RegExp(lineageName) })).toHaveProperty(
      "disabled",
      false
    );
    expect(row.getByText("The first admitted member.")).toBeTruthy();
    expect(row.getByRole("radio").closest("label")?.textContent).not.toMatch(/cannot be started/i);
  });

  it("asks the existing by-name door for the head of a name that has several revisions", async () => {
    const cockpitApi = lineageApi();
    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });
    await screen.findByRole("radio", { name: new RegExp(lineageName) });

    expect(vi.mocked(cockpitApi.getRevisionByName).mock.calls).toEqual([[lineageName]]);
  });

  it("proves(an-unadmitted-or-uncatalogable-published-name-is-named-in-the-picker): names a legal missing name as unlisted", async () => {
    const legalName = "diff-review";
    const cockpitApi = api(
      [
        decodedRow({
          revision_hash: namedHash,
          format_version: 3,
          executable: true,
          not_executable_reason: null,
          name: legalName,
          description: null
        })
      ],
      {
        getRevisionByName: vi.fn(async () => {
          throw new CockpitRequestError(
            "No lineage of this kind holds that name at that position.",
            {
              type: "urn:atelier2:problem:v1:catalog-name-not-found",
              title: "Catalog name not found",
              status: 404,
              detail: "No lineage of this kind holds that name at that position."
            },
            true
          );
        })
      }
    );
    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    await screen.findByRole("radio", { name: new RegExp(legalName) });
    expect(screen.getByText("Unlisted")).toBeTruthy();
    expect(vi.mocked(cockpitApi.getRevisionByName).mock.calls).toEqual([[legalName]]);
  });

  it("proves(an-unadmitted-or-uncatalogable-published-name-is-named-in-the-picker): names the live illegal title without asking the catalog", async () => {
    const cockpitApi = api([namedRevision()]);
    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    await screen.findByRole("radio", {
      name: /Implement a candidate, then review it for defects/
    });
    expect(screen.getByText("Unnamable")).toBeTruthy();
    expect(cockpitApi.getRevisionByName).not.toHaveBeenCalled();
  });

  it("does not show an empty revision submenu for a lineage with one revision", async () => {
    renderPicker([namedRevision()]);

    await screen.findByRole("radio", {
      name: /Implement a candidate, then review it for defects/
    });

    expect(screen.queryByLabelText(/Revisions of/)).toBeNull();
    expect(screen.queryByLabelText(/^Revision of /)).toBeNull();
  });
});
