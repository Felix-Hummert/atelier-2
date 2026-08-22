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
    workflow_revision_hash: namedHash,
    workflow_format_version: 3,
    executable: false,
    not_executable_reason:
      "agent-output-shape-unavailable: 0 outputs on node 'implement', and an agent node completes with the one value its own schema judges",
    name: "Implement a candidate, then review it for defects",
    description: "Builds the candidate, then reviews it for defects."
  });

const unnamedRevision = () =>
  decodedRow({
    workflow_revision_hash: unnamedHash,
    workflow_format_version: 2,
    executable: true,
    not_executable_reason: null,
    name: null,
    description: null
  });

function savedRevision(hash: string, name: string, description: string) {
  return decodedRow({
    workflow_revision_hash: hash,
    workflow_format_version: 3,
    executable: true,
    not_executable_reason: null,
    name,
    description
  });
}

function catalogHead(name: string, hash: string, revisionNumber: number) {
  return {
    display_name: name,
    lineage_id: "e".repeat(64),
    workflow_revision_hash: hash,
    revision_number: revisionNumber
  };
}

/**
 * The graph the detail route already publishes for the named V3 row.
 *
 * Roles and node count live here, not on the listing. The document bytes are a
 * trap: Details must repeat these fields and must not parse that payload.
 */
function namedGraph() {
  return {
    workflow_format_version: 3 as const,
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
    loops: [],
    name: "Implement a candidate, then review it for defects",
    description: "Builds the candidate, then reviews it for defects."
  };
}

function namedDetail() {
  return {
    workflow_revision_hash: namedHash,
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
  it("repeats only an unavailable saved-workflow read until truth confirms", async () => {
    const listWorkflowRevisions = vi
      .fn()
      .mockRejectedValueOnce(new Error("first private workflow detail"))
      .mockRejectedValueOnce(new Error("second private workflow detail"))
      .mockResolvedValueOnce({
        items: [unnamedRevision()],
        next_after_revision_hash: null
      });
    const cockpitApi = cockpitApiStub({ listWorkflowRevisions });
    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    await screen.findByText("Saved workflows unavailable");
    // A fresh query per click, never a held reference: Retry mounts its own
    // control each failed round (ReadState.svelte's pattern for #514), so the
    // operator clicks whatever Retry is on screen right now.
    expect(screen.queryByText(/private workflow detail|Failed to fetch/)).toBeNull();
    expect(screen.getAllByRole("button", { name: "Retry saved workflows" })).toHaveLength(1);

    await fireEvent.click(screen.getByRole("button", { name: "Retry saved workflows" }));
    await waitFor(() => expect(listWorkflowRevisions).toHaveBeenCalledTimes(2));
    expect(screen.getAllByRole("button", { name: "Retry saved workflows" })).toHaveLength(1);
    expect(screen.queryByText(/private workflow detail|Failed to fetch/)).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Retry saved workflows" }));

    expect((await screen.findByRole("radio", { name: /unnamed/i })).isConnected).toBe(true);
    expect(listWorkflowRevisions).toHaveBeenCalledTimes(3);
    expect(cockpitApi.listAgentConfigurationRevisions).toHaveBeenCalledTimes(1);
    expect(cockpitApi.getRevisionByName).not.toHaveBeenCalled();
    expect(cockpitApi.getWorkflowRevision).not.toHaveBeenCalled();
    expect(cockpitApi.publish).not.toHaveBeenCalled();
    expect(cockpitApi.start).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /saved workflows/ })).toBeNull();
    expect(window.location.pathname).toBe("/atelier/new");
  });

  it("does not confirm a partial saved-workflow page", async () => {
    const listWorkflowRevisions = vi
      .fn()
      .mockResolvedValueOnce({
        items: [unnamedRevision()],
        next_after_revision_hash: unnamedHash
      })
      .mockRejectedValueOnce(new Error("private later-page detail"))
      .mockResolvedValueOnce({
        items: [namedRevision()],
        next_after_revision_hash: null
      });
    const cockpitApi = cockpitApiStub({ listWorkflowRevisions });
    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    await screen.findByText("Saved workflows incomplete");
    expect(screen.queryByRole("radio", { name: /unnamed/i })).toBeNull();
    expect(screen.queryByText(/private later-page detail/)).toBeNull();
    expect(listWorkflowRevisions).toHaveBeenNthCalledWith(2, unnamedHash);

    await fireEvent.click(screen.getByRole("button", { name: "Retry saved workflows" }));

    expect(
      (await screen.findByRole("radio", {
        name: /Implement a candidate, then review it for defects/
      })).isConnected
    ).toBe(true);
    expect(screen.queryByRole("radio", { name: /unnamed/i })).toBeNull();
  });

  it("does not confirm an admitted catalog head absent from the same named listing", async () => {
    const listedName = "listed-line";
    const listedHash = "6".repeat(64);
    const absentHeadHash = "7".repeat(64);
    const getRevisionByName = vi
      .fn()
      .mockResolvedValueOnce(catalogHead(listedName, absentHeadHash, 2))
      .mockResolvedValueOnce(catalogHead(listedName, listedHash, 1));
    const cockpitApi = api(
      [savedRevision(listedHash, listedName, "The listed revision.")],
      { getRevisionByName }
    );
    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    await screen.findByText("Saved workflows unavailable");
    expect(screen.queryByRole("article", { name: listedName })).toBeNull();
    expect(screen.getAllByRole("button", { name: "Retry saved workflows" })).toHaveLength(1);

    await fireEvent.click(screen.getByRole("button", { name: "Retry saved workflows" }));

    expect((await screen.findByRole("article", { name: listedName })).isConnected).toBe(true);
    expect(cockpitApi.listWorkflowRevisions).toHaveBeenCalledTimes(2);
    expect(getRevisionByName).toHaveBeenCalledTimes(2);
    expect(cockpitApi.listAgentConfigurationRevisions).toHaveBeenCalledTimes(1);
  });

  it("names the empty listing instead of offering a silent choice", async () => {
    renderPicker([]);

    expect(await screen.findByText("No saved workflows yet.")).toBeTruthy();
    expect(screen.queryByRole("radio", { name: /saved-revision|unnamed/i })).toBeNull();
  });

  it("offers a named workflow by its name and keeps the exact hash under details", async () => {
    renderPicker([namedRevision()]);

    const option = await screen.findByRole("radio", {
      name: /Implement a candidate, then review it for defects/
    });

    expect(option).toBeTruthy();
    expect(option.closest("label")?.textContent).not.toContain(namedHash);
    expect(screen.getByText("Builds the candidate, then reviews it for defects.")).toBeTruthy();
    const details = screen.getByText("Details").closest("details");
    expect(details?.open).toBe(false);
    expect(details?.textContent).not.toContain(namedHash);
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
    expect(details?.textContent).not.toContain(namedHash);
    await fireEvent.click(screen.getByRole("button", { name: "Workflow revision" }));
    expect(details?.textContent).toContain(namedHash);
    expect(details?.textContent).not.toBe(namedHash);
    expect(details?.textContent).not.toContain("NEVER_PARSE_THIS_INSTRUCTION");
    expect(vi.mocked(cockpitApi.getWorkflowRevision).mock.calls.map(([hash]) => hash)).toEqual([
      namedHash
    ]);
  });

  it("retains one failed detail read until exact Retry confirms it, then Edit reuses that immutable truth", async () => {
    const getWorkflowRevision = vi
      .fn()
      .mockRejectedValueOnce(new Error("first private detail"))
      .mockRejectedValueOnce(new Error("second private detail"))
      .mockResolvedValueOnce(namedDetail());
    const cockpitApi = api([namedRevision()], { getWorkflowRevision });
    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });
    await screen.findByRole("radio", {
      name: /Implement a candidate, then review it for defects/
    });

    await fireEvent.click(screen.getByText("Details"));
    expect(await screen.findByText("Workflow detail unavailable")).toBeTruthy();
    expect(screen.queryByText(/private detail|Failed to fetch/)).toBeNull();
    expect(screen.getAllByRole("button", { name: "Retry workflow detail" })).toHaveLength(1);
    expect(getWorkflowRevision).toHaveBeenCalledTimes(1);

    // A fresh query per click, never a held reference: Retry mounts its own
    // control each failed round (ReadState.svelte's pattern for #514).
    await fireEvent.click(screen.getByRole("button", { name: "Retry workflow detail" }));
    await waitFor(() => expect(getWorkflowRevision).toHaveBeenCalledTimes(2));
    expect(screen.getAllByRole("button", { name: "Retry workflow detail" })).toHaveLength(1);
    expect(screen.queryByText(/private detail|Failed to fetch/)).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Retry workflow detail" }));
    await waitFor(() => expect(screen.getByText("Details").closest("details")?.textContent).toContain("builder"));
    await fireEvent.click(screen.getByRole("button", { name: "Edit" }));

    expect((await screen.findByLabelText("Exact workflow YAML") as HTMLTextAreaElement).value).toBe(
      "job: NEVER_PARSE_THIS_INSTRUCTION\n"
    );
    expect(getWorkflowRevision.mock.calls.map(([hash]) => hash)).toEqual([
      namedHash,
      namedHash,
      namedHash
    ]);
    expect(screen.queryByRole("button", { name: "Refresh workflow detail" })).toBeNull();
  });

  it("retains the confirmed revision and draft until a mismatched revision choice retries exact", async () => {
    const name = "Revision recovery proof";
    const confirmedHash = "1".repeat(64);
    const attemptedHash = "2".repeat(64);
    const detail = (hash: string, document: string) => ({
      workflow_revision_hash: hash,
      document_base64: utf8Base64(document),
      graph: {
        ...namedGraph(),
        executable: true as const,
        not_executable_reason: null,
        name,
        description: "Keeps one exact saved revision."
      }
    });
    const confirmed = detail(confirmedHash, "format_version: 3\nname: confirmed\n");
    const attempted = detail(attemptedHash, "format_version: 3\nname: attempted\n");
    const getWorkflowRevision = vi
      .fn()
      .mockResolvedValueOnce(confirmed)
      .mockResolvedValueOnce(confirmed)
      .mockResolvedValueOnce(attempted);
    const cockpitApi = api(
      [
        savedRevision(confirmedHash, name, "Confirmed revision."),
        savedRevision(attemptedHash, name, "Attempted revision.")
      ],
      {
        getRevisionByName: vi.fn(async () => catalogHead(name, confirmedHash, 2)),
        getWorkflowRevision
      }
    );
    const createRunId = vi.fn().mockReturnValueOnce("run-confirmed").mockReturnValueOnce("run-attempted");
    render(App, {
      props: {
        cockpitApi,
        mutationJournal: new MutationJournal(sessionStorage),
        createRunId
      }
    });

    await fireEvent.click(await screen.findByRole("radio", { name: new RegExp(name) }));
    expect(await screen.findByText("run-confirmed")).toBeTruthy();
    await fireEvent.click(screen.getByText("Details"));
    const choice = screen.getByLabelText(`Revision of ${name}`) as HTMLSelectElement;
    await fireEvent.change(choice, { target: { value: attemptedHash } });

    expect(await screen.findByText("Workflow detail unavailable")).toBeTruthy();
    expect(choice.value).toBe(confirmedHash);
    expect(screen.getByText("run-confirmed").isConnected).toBe(true);
    expect(screen.queryByText("run-attempted")).toBeNull();
    expect(screen.getAllByRole("button", { name: "Retry workflow detail" })).toHaveLength(1);

    await fireEvent.click(screen.getByRole("button", { name: "Retry workflow detail" }));
    await waitFor(() => expect(choice.value).toBe(attemptedHash));
    expect(screen.getByText("run-attempted").isConnected).toBe(true);
    expect(screen.queryByText("run-confirmed")).toBeNull();
    expect(getWorkflowRevision.mock.calls.map(([hash]) => hash)).toEqual([
      confirmedHash,
      attemptedHash,
      attemptedHash
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
          workflow_revision_hash: namedHash,
          workflow_format_version: 3,
          executable: false,
          not_executable_reason:
      "agent-output-shape-unavailable: 0 outputs on node 'implement', and an agent node completes with the one value its own schema judges",
          name: "Implement a candidate, then review it for defects",
          description: "Builds the candidate, then reviews it for defects."
        })
      ],
      {
        getWorkflowRevision: vi.fn(async () => ({
          workflow_revision_hash: namedHash,
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
    expect(screen.getByLabelText("Details for this unnamed workflow")).toBeTruthy();
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
      const index = after === undefined ? 0 : pages.findIndex((page) => page.at(-1)?.workflow_revision_hash === after) + 1;
      const items = pages[index] ?? [];
      const last = index + 1 < pages.length ? items.at(-1)?.workflow_revision_hash ?? null : null;
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
      workflow_revision_hash: "c".repeat(64),
      workflow_format_version: 3,
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
      workflow_revision_hash: olderHash,
      workflow_format_version: 3,
      executable: true,
      not_executable_reason: null,
      name: lineageName,
      description: "The first admitted member."
    });
  }

  function newestRevision() {
    return decodedRow({
      workflow_revision_hash: newestHash,
      workflow_format_version: 3,
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
        workflow_revision_hash: newestHash,
        revision_number: 2
      })),
      ...overrides
    });
  }

  it("offers two revisions of one lineage as one row, defaults to the newest, and switching revision changes startability", async () => {
    render(App, {
      props: {
        cockpitApi: lineageApi({
          getWorkflowRevision: vi.fn(async (hash: string) => ({
            workflow_revision_hash: hash,
            document_base64: "",
            graph: {
              ...namedGraph(),
              executable: true as const,
              not_executable_reason: null,
              name: lineageName,
              description: "The first admitted member."
            }
          }))
        }),
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

    await fireEvent.click(row.getByText("Details"));
    await fireEvent.change(row.getByLabelText(`Revision of ${lineageName}`), {
      target: { value: olderHash }
    });

    await waitFor(() => expect(
      screen.getByRole("radio", { name: new RegExp(lineageName) })
    ).toHaveProperty("disabled", false));
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

  it("confirms every row's catalog head only together -- one name's failed head read confirms none, and Retry confirms all", async () => {
    const readyName = "ready-line";
    const failingName = "failing-line";
    const readyHash = "1".repeat(64);
    const failingHash = "2".repeat(64);
    const ready = savedRevision(readyHash, readyName, "A catalog head that always reads.");
    const failing = savedRevision(failingHash, failingName, "A catalog head that fails once.");
    const listWorkflowRevisions = vi.fn(async () => ({
      items: [ready, failing],
      next_after_revision_hash: null
    }));
    let failingNameReads = 0;
    const getRevisionByName = vi.fn(async (name: string) => {
      if (name === readyName) return catalogHead(name, readyHash, 1);
      failingNameReads += 1;
      if (failingNameReads === 1) throw new Error("private catalog head detail");
      return catalogHead(name, failingHash, 1);
    });
    const cockpitApi = cockpitApiStub({ listWorkflowRevisions, getRevisionByName });
    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    await screen.findByText("Saved workflows unavailable");
    expect(screen.queryByRole("article", { name: readyName })).toBeNull();
    expect(screen.queryByText(/private catalog head detail/)).toBeNull();
    expect(screen.getAllByRole("button", { name: "Retry saved workflows" })).toHaveLength(1);

    await fireEvent.click(screen.getByRole("button", { name: "Retry saved workflows" }));

    const readyRow = await screen.findByRole("article", { name: readyName });
    expect(readyRow.getAttribute("data-catalog-form")).toBe("ready");
    expect(
      screen.getByRole("article", { name: failingName }).getAttribute("data-catalog-form")
    ).toBe("ready");
    expect(cockpitApi.listAgentConfigurationRevisions).toHaveBeenCalledTimes(1);
    expect(cockpitApi.getWorkflowRevision).not.toHaveBeenCalled();
    expect(cockpitApi.publish).not.toHaveBeenCalled();
    expect(cockpitApi.start).not.toHaveBeenCalled();
  });

  it("proves(an-unadmitted-or-uncatalogable-published-name-is-named-in-the-picker): names a legal missing name as unlisted", async () => {
    const legalName = "diff-review";
    const cockpitApi = api(
      [
        decodedRow({
          workflow_revision_hash: namedHash,
          workflow_format_version: 3,
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

  it("keeps a retired catalog name as domain truth instead of a read failure", async () => {
    const retiredName = "retired-line";
    const cockpitApi = api(
      [savedRevision(namedHash, retiredName, "A retired catalog line.")],
      {
        getRevisionByName: vi.fn(async () => {
          throw new CockpitRequestError(
            "private retirement detail",
            {
              type: "urn:atelier2:problem:v1:catalog-lineage-retired",
              title: "Catalog lineage retired",
              status: 410,
              detail: "private retirement detail"
            },
            true
          );
        })
      }
    );
    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    const retired = await screen.findByRole("article", { name: retiredName });
    expect(retired.getAttribute("data-catalog-form")).toBe("retired");
    expect(within(retired).getByText("Retired").isConnected).toBe(true);
    expect(screen.queryByText("Saved workflows unavailable")).toBeNull();
    expect(screen.queryByText(/private retirement detail/)).toBeNull();
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

  it("proves(a-picker-row-carries-its-catalog-state-in-its-shape): ready, unlisted, unnamable and refused rows differ by form", async () => {
    const readyName = "ready-line";
    const unlistedName = "unlisted-line";
    const cockpitApi = api(
      [
        decodedRow({
          workflow_revision_hash: "1".repeat(64),
          workflow_format_version: 3,
          executable: true,
          not_executable_reason: null,
          name: readyName,
          description: null
        }),
        decodedRow({
          workflow_revision_hash: "2".repeat(64),
          workflow_format_version: 3,
          executable: true,
          not_executable_reason: null,
          name: unlistedName,
          description: null
        }),
        decodedRow({
          workflow_revision_hash: "5".repeat(64),
          workflow_format_version: 3,
          executable: true,
          not_executable_reason: null,
          name: "Der erste Lauf",
          description: null
        }),
        namedRevision()
      ],
      {
        getRevisionByName: vi.fn(async (asked: string) => {
          if (asked === readyName) {
            return {
              display_name: readyName,
              lineage_id: "e".repeat(64),
              workflow_revision_hash: "1".repeat(64),
              revision_number: 1
            };
          }
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

    const ready = await screen.findByRole("article", { name: readyName });
    const unlisted = await screen.findByRole("article", { name: unlistedName });
    const unnamable = await screen.findByRole("article", { name: "Der erste Lauf" });
    const refused = screen.getByRole("article", {
      name: "Implement a candidate, then review it for defects"
    });
    await waitFor(() => expect(unlisted.getAttribute("data-catalog-form")).toBe("unlisted"));
    expect(ready.getAttribute("data-catalog-form")).toBe("ready");
    expect(unnamable.getAttribute("data-catalog-form")).toBe("unnamable");
    expect(refused.getAttribute("data-catalog-form")).toBe("refused");
    expect(refused.getAttribute("data-catalog-form")).not.toBe(
      unlisted.getAttribute("data-catalog-form")
    );
    expect(ready.getAttribute("data-catalog-form")).not.toBe(
      refused.getAttribute("data-catalog-form")
    );
  });

  it("proves(a-chosen-workflow-collapses-the-picker-onto-its-start): hides the rest of the list and sits the start form under the chosen card", async () => {
    const first = "first-line";
    const second = "second-line";
    const firstHash = "3".repeat(64);
    const cockpitApi = api(
      [
        decodedRow({
          workflow_revision_hash: firstHash,
          workflow_format_version: 3,
          executable: true,
          not_executable_reason: null,
          name: first,
          description: null
        }),
        decodedRow({
          workflow_revision_hash: "4".repeat(64),
          workflow_format_version: 3,
          executable: true,
          not_executable_reason: null,
          name: second,
          description: null
        })
      ],
      {
        getRevisionByName: vi.fn(async (asked: string) => ({
          display_name: asked,
          lineage_id: "e".repeat(64),
          workflow_revision_hash: asked === first ? firstHash : "4".repeat(64),
          revision_number: 1
        })),
        getWorkflowRevision: vi.fn(async () => ({
          workflow_revision_hash: firstHash,
          document_base64: "YQ==",
          graph: {
            workflow_format_version: 3 as const,
            executable: true as const,
            not_executable_reason: null,
            node_count: 1,
            agent_roles: [] as string[],
            orders: [],
            node_previews: [
              {
                id: "only",
                kind: "wait" as const,
                role: null,
                instruction_start: null,
                depends_on: []
              }
            ],
            loops: [],
            name: first,
            description: null
          }
        }))
      }
    );
    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    await screen.findByRole("article", { name: second });
    await fireEvent.click(screen.getByRole("radio", { name: new RegExp(first) }));

    await screen.findByRole("heading", { name: "Run ID" });
    expect(screen.queryByRole("article", { name: second })).toBeNull();
    expect(screen.getByRole("article", { name: first }).isConnected).toBe(true);
    expect(screen.getByRole("button", { name: "Change" }).isConnected).toBe(true);

    await fireEvent.click(screen.getByRole("button", { name: "Change" }));

    expect((await screen.findByRole("article", { name: second })).isConnected).toBe(true);
    expect(screen.queryByRole("heading", { name: "Run ID" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Change" })).toBeNull();
  });

  it("proves(a-details-panel-shows-the-published-substance): names declared orders and an honest empty when the revision declares none", async () => {
    const withOrders = {
      ...namedDetail(),
      graph: {
        ...namedGraph(),
        orders: [
          {
            name: "portions",
            schema: {
              ref: "portions-schema",
              revision: "e".repeat(64)
            }
          }
        ]
      }
    };
    const cockpitApi = api([namedRevision()], {
      getWorkflowRevision: vi.fn(async () => withOrders)
    });
    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });
    await screen.findByRole("radio", {
      name: /Implement a candidate, then review it for defects/
    });
    await fireEvent.click(screen.getByText("Details"));

    const orders = await screen.findByRole("region", { name: "Orders" });
    expect(orders.textContent).toContain("portions");
    expect(orders.textContent).toContain("portions-schema");
    expect(orders.textContent).not.toContain("e".repeat(64));
    await fireEvent.click(screen.getByRole("button", { name: "Schema of portions" }));
    expect(orders.textContent).toContain("e".repeat(64));
    expect(screen.queryByText("No orders.")).toBeNull();
  });

  it("proves(a-revision-hash-is-a-proof-anchor): hides the revision hash until asked and copies it", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.assign(globalThis.navigator, { clipboard: { writeText } });
    const cockpitApi = api([namedRevision()], {
      getWorkflowRevision: vi.fn(async () => namedDetail())
    });
    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });
    await screen.findByRole("radio", {
      name: /Implement a candidate, then review it for defects/
    });
    await fireEvent.click(screen.getByText("Details"));

    const details = screen.getByText("Details").closest("details");
    expect(details?.textContent).not.toContain(namedHash);
    await fireEvent.click(screen.getByRole("button", { name: "Workflow revision" }));
    expect(details?.textContent).toContain(namedHash);
    expect(screen.getByText("Seals the published document.")).toBeTruthy();
    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText).toHaveBeenCalledWith(namedHash);
    await waitFor(() => expect(screen.getByText("Copied").isConnected).toBe(true));
  });

  it("proves(a-published-document-can-be-edited-into-a-new-revision): shows the exact YAML and publishes it through the existing door", async () => {
    const original = "job: NEVER_PARSE_THIS_INSTRUCTION\n";
    const edited = "format_version: 3\nname: edited-line\n";
    const newHash = "f".repeat(64);
    const cockpitApi = api([namedRevision()], {
      getWorkflowRevision: vi.fn(async () => namedDetail()),
      publish: vi.fn(async () => ({
        status: 201,
        value: {
          workflow_revision_hash: newHash,
          document_base64: utf8Base64(edited),
          graph: {
            ...namedGraph(),
            name: "edited-line",
            executable: true,
            not_executable_reason: null
          }
        }
      })),
      foundCatalogLineage: vi.fn(async () => ({
        status: 201,
        value: {
          display_name: "edited-line",
          lineage_id: "e".repeat(64),
          workflow_revision_hash: newHash,
          revision_number: 1
        }
      }))
    });
    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });
    await screen.findByRole("radio", {
      name: /Implement a candidate, then review it for defects/
    });
    await fireEvent.click(screen.getByText("Details"));
    await fireEvent.click(await screen.findByRole("button", { name: "Edit" }));

    const editor = await screen.findByLabelText("Exact workflow YAML");
    expect((editor as HTMLTextAreaElement).value).toBe(original);
    await fireEvent.input(editor, { target: { value: edited } });
    await fireEvent.click(screen.getByRole("button", { name: "Review publication" }));
    const dialog = screen.getByRole("dialog", { name: "Publish this exact workflow?" });
    await fireEvent.click(within(dialog).getByRole("button", { name: "Publish" }));

    await waitFor(() => expect(cockpitApi.publish).toHaveBeenCalledTimes(1));
    expect(vi.mocked(cockpitApi.foundCatalogLineage)).toHaveBeenCalledTimes(1);
  });
});
