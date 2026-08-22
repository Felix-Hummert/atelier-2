import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  CockpitRequestError,
  type CatalogNameResolution,
  type CockpitApi,
  type Problem,
  type WorkflowRevisionDetail,
  type WorkflowRevisionSummary
} from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { cockpitApiStub } from "../support/cockpitApi";

type V3Graph = Extract<WorkflowRevisionDetail["graph"], { workflow_format_version: 3 }>;
type GetRevisionByName = CockpitApi["getRevisionByName"];

const NAMED_HASH = "b".repeat(64);
const LINEAGE_ID = "e".repeat(64);
const UNNAMED_HASH = "c".repeat(64);
const WORKFLOW_NAME = "iterate-code";

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
});

function openAt(pathname: string, overrides: Partial<CockpitApi> = {}) {
  window.history.replaceState(null, "", pathname);
  return render(App, {
    props: {
      cockpitApi: cockpitApiStub(overrides),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
}

function namedSummary(overrides: Partial<WorkflowRevisionSummary> = {}): WorkflowRevisionSummary {
  return {
    workflow_revision_hash: NAMED_HASH,
    workflow_format_version: 3,
    executable: true,
    not_executable_reason: null,
    name: WORKFLOW_NAME,
    description: "build, then review",
    ...overrides
  };
}

function unnamedSummary(): WorkflowRevisionSummary {
  return {
    workflow_revision_hash: UNNAMED_HASH,
    workflow_format_version: 1,
    executable: true,
    not_executable_reason: null,
    name: null,
    description: null
  };
}

function namedDetail(graphOverrides: Partial<V3Graph> = {}, hash = NAMED_HASH): WorkflowRevisionDetail {
  const graph: V3Graph = {
    workflow_format_version: 3,
    executable: true,
    not_executable_reason: null,
    node_count: 2,
    agent_roles: ["builder", "reviewer"],
    orders: [],
    node_previews: [
      { id: "build", kind: "agent", role: "builder", instruction_start: "Write the change.", depends_on: [] },
      { id: "review", kind: "agent", role: "reviewer", instruction_start: "Check the diff.", depends_on: ["build"] }
    ],
    loops: [],
    name: WORKFLOW_NAME,
    description: "build, then review",
    ...graphOverrides
  };
  return { workflow_revision_hash: hash, document_base64: "", graph };
}

/** `getRevisionByName` answering that this name's catalog head is `hash`. */
function admittedByName(hash = NAMED_HASH): GetRevisionByName {
  return vi.fn(async (): Promise<CatalogNameResolution> => ({
    display_name: WORKFLOW_NAME,
    lineage_id: LINEAGE_ID,
    workflow_revision_hash: hash,
    revision_number: 1
  }));
}

function problem(code: string): Problem {
  return {
    type: `urn:atelier2:problem:v1:${code}`,
    title: code,
    status: code === "catalog-lineage-retired" ? 410 : 404,
    detail: code
  } as Problem;
}

/** `getRevisionByName` answering that this name's lineage was retired. */
function retiredByName(): GetRevisionByName {
  return vi.fn(async () => {
    throw new CockpitRequestError("retired", problem("catalog-lineage-retired"), true);
  });
}

describe("the workflows catalog list", () => {
  it("shows a card for each named workflow and its one-line description", async () => {
    openAt("/atelier/workflows", {
      listWorkflowRevisions: vi.fn(async () => ({
        items: [namedSummary(), unnamedSummary()],
        next_after_revision_hash: null
      })),
      getRevisionByName: admittedByName()
    });

    const card = await screen.findByRole("button", { name: /iterate-code/ });
    expect(card.textContent).toContain("build, then review");
  });

  it("never shows the unnamed revision -- the library shows names, never hashes", async () => {
    openAt("/atelier/workflows", {
      listWorkflowRevisions: vi.fn(async () => ({
        items: [unnamedSummary()],
        next_after_revision_hash: null
      }))
    });

    await screen.findByText("No named workflows yet");
    expect(screen.queryByText(UNNAMED_HASH)).toBeNull();
  });

  it("renders the catalog head, not whatever order the list answered with", async () => {
    const staleFirst = namedSummary({
      workflow_revision_hash: "a".repeat(64),
      description: "an older, no-longer-current revision"
    });
    const catalogHead = namedSummary({
      workflow_revision_hash: "d".repeat(64),
      description: "the current, catalog-admitted revision"
    });

    openAt("/atelier/workflows", {
      // The list answers with the stale revision first -- the durable store
      // orders by hash, not recency -- so a card that trusted list order
      // would show the wrong description.
      listWorkflowRevisions: vi.fn(async () => ({
        items: [staleFirst, catalogHead],
        next_after_revision_hash: null
      })),
      getRevisionByName: admittedByName(catalogHead.workflow_revision_hash)
    });

    const card = await screen.findByRole("button", { name: /iterate-code/ });
    expect(card.textContent).toContain("the current, catalog-admitted revision");
    expect(card.textContent).not.toContain("an older, no-longer-current revision");
  });

  it("shows a name with no catalog admission rather than hiding it", async () => {
    openAt("/atelier/workflows", {
      listWorkflowRevisions: vi.fn(async () => ({
        items: [namedSummary()],
        next_after_revision_hash: null
      })),
      getRevisionByName: vi.fn(async () => {
        throw new CockpitRequestError("missing", problem("catalog-name-not-found"), true);
      })
    });

    const card = await screen.findByRole("button", { name: /iterate-code/ });
    expect(card.textContent).toContain("Not admitted to the catalog.");
  });

  it("opens a workflow's detail page on a card click", async () => {
    openAt("/atelier/workflows", {
      listWorkflowRevisions: vi.fn(async () => ({
        items: [namedSummary()],
        next_after_revision_hash: null
      })),
      getRevisionByName: admittedByName(),
      getWorkflowRevision: vi.fn(async () => namedDetail())
    });

    fireEvent.click(await screen.findByRole("button", { name: /iterate-code/ }));

    expect((await screen.findByRole("heading", { name: WORKFLOW_NAME })).isConnected).toBe(true);
  });
});

describe("the workflow detail", () => {
  function openDetail(overrides: Partial<CockpitApi> = {}) {
    return openAt(`/atelier/workflows/${encodeURIComponent(WORKFLOW_NAME)}`, {
      listWorkflowRevisions: vi.fn(async () => ({
        items: [namedSummary()],
        next_after_revision_hash: null
      })),
      getRevisionByName: admittedByName(),
      getWorkflowRevision: vi.fn(async () => namedDetail()),
      ...overrides
    });
  }

  it("draws the still graph and opens a node's role and prompt template on click", async () => {
    openDetail();

    fireEvent.click(await screen.findByRole("button", { name: "review" }));

    expect((await screen.findByRole("heading", { name: "review" })).isConnected).toBe(true);
    expect(screen.getByText("reviewer").isConnected).toBe(true);
    expect(screen.getByText("Check the diff.").isConnected).toBe(true);
  });

  it("shows the catalog head, not whatever order the list answered with", async () => {
    const staleFirst = namedSummary({
      workflow_revision_hash: "a".repeat(64),
      description: "an older, no-longer-current revision"
    });
    const catalogHead = namedSummary({
      workflow_revision_hash: "d".repeat(64),
      description: "the current, catalog-admitted revision"
    });

    openDetail({
      listWorkflowRevisions: vi.fn(async () => ({
        items: [staleFirst, catalogHead],
        next_after_revision_hash: null
      })),
      getRevisionByName: admittedByName(catalogHead.workflow_revision_hash),
      getWorkflowRevision: vi.fn(async (hash: string) =>
        namedDetail({ description: hash === catalogHead.workflow_revision_hash ? "current" : "stale" }, hash)
      )
    });

    expect((await screen.findByText("current")).isConnected).toBe(true);
    expect(screen.queryByText("stale")).toBeNull();
  });

  it("shows a retired lineage's state and refuses to offer Start on it", async () => {
    openDetail({ getRevisionByName: retiredByName() });

    expect((await screen.findByText("Retired")).isConnected).toBe(true);
    const start = (await screen.findByRole("button", { name: "Start" })) as HTMLButtonElement;
    expect(start.disabled).toBe(true);
    expect(
      (await screen.findByText(/catalog lineage was retired/)).isConnected
    ).toBe(true);
  });

  it("disables Start and names the refusal when the revision cannot run", async () => {
    openDetail({
      getWorkflowRevision: vi.fn(async () =>
        namedDetail({ executable: false, not_executable_reason: "agent forms nothing binds yet: outputs" })
      )
    });

    const start = (await screen.findByRole("button", { name: "Start" })) as HTMLButtonElement;
    expect(start.disabled).toBe(true);
    expect((await screen.findByText(/Cannot be started/)).isConnected).toBe(true);
  });

  it("sends Start to the existing start door rather than rebuilding it here", async () => {
    openDetail();
    await screen.findByRole("heading", { name: WORKFLOW_NAME });

    fireEvent.click(screen.getByRole("button", { name: "Start" }));

    expect((await screen.findByRole("heading", { name: "Choose a workflow" })).isConnected).toBe(true);
  });

  it("names a workflow nothing publishes instead of showing a stale or empty graph", async () => {
    openAt(`/atelier/workflows/${encodeURIComponent("does-not-exist")}`, {
      listWorkflowRevisions: vi.fn(async () => ({
        items: [namedSummary()],
        next_after_revision_hash: null
      })),
      getRevisionByName: vi.fn(async () => {
        throw new CockpitRequestError("missing", problem("catalog-name-not-found"), true);
      })
    });

    expect((await screen.findByText("Workflow not found")).isConnected).toBe(true);
  });
});
