import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  CockpitRequestError,
  type AgentDefinitionRevisionListItem,
  type CockpitApi,
  type Problem,
  type WorkflowRevisionSummary
} from "../../src/api/client";
import { catalogPageCopy } from "../../src/lib/catalogPageCopy";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { cockpitApiStub } from "../support/cockpitApi";

const WORKFLOW_HASH = "b".repeat(64);
const SIBLING_HASH = "f".repeat(64);
const AGENT_HASH = "d".repeat(64);
const LINEAGE_ID = "e".repeat(64);
const WORKFLOW_NAME = "iterate-code";
const EXACT_YAML = "format_version: 3\nname: iterate-code\n";
const EXACT_AGENT = "---\nname: scribe\ndescription: Writes.\n---\n\nYou write.\n";

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
});

function openCatalog(overrides: Partial<CockpitApi> = {}) {
  window.history.replaceState(null, "", "/atelier/catalog");
  return render(App, {
    props: {
      cockpitApi: cockpitApiStub(overrides),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
}

function workflowSummary(
  overrides: Partial<WorkflowRevisionSummary> = {}
): WorkflowRevisionSummary {
  return {
    workflow_revision_hash: WORKFLOW_HASH,
    workflow_format_version: 3,
    executable: true,
    not_executable_reason: null,
    name: WORKFLOW_NAME,
    description: "build, then review",
    ...overrides
  };
}

function agentItem(
  overrides: Partial<AgentDefinitionRevisionListItem> = {}
): AgentDefinitionRevisionListItem {
  return {
    agent_definition_revision_hash: AGENT_HASH,
    name: "scribe",
    description: "Writes what the stage needs.",
    ...overrides
  };
}

function listing(items: readonly WorkflowRevisionSummary[]): Partial<CockpitApi> {
  return {
    listWorkflowRevisions: vi.fn(async () => ({
      items: [...items],
      next_after_revision_hash: null
    }))
  };
}

function admittedName(): Partial<CockpitApi> {
  return {
    getRevisionByName: vi.fn(async () => ({
      display_name: WORKFLOW_NAME,
      lineage_id: LINEAGE_ID,
      workflow_revision_hash: WORKFLOW_HASH,
      revision_number: 1
    }))
  };
}

function unlistedName(): Partial<CockpitApi> {
  return {
    getRevisionByName: vi.fn(async () => {
      throw new CockpitRequestError("no such name", {
        type: "urn:atelier2:problem:v1:catalog-name-not-found",
        title: "Catalog name not found",
        status: 404,
        detail: "no such name"
      } as Problem);
    })
  };
}

describe("the catalog room", () => {
  it("greets an empty atelier by naming the door that fills it", async () => {
    openCatalog();

    expect(
      (await screen.findByText(catalogPageCopy.workflowsEmpty)).isConnected
    ).toBe(true);
    expect((await screen.findByText(catalogPageCopy.agentsEmpty)).isConnected).toBe(true);
  });

  it("says skills cannot arrive yet instead of showing an empty list", async () => {
    openCatalog();

    expect((await screen.findByText(catalogPageCopy.skillsNone)).isConnected).toBe(true);
  });

  it("shows a published workflow with where it came from", async () => {
    openCatalog({ ...listing([workflowSummary()]), ...admittedName() });

    expect((await screen.findByText(WORKFLOW_NAME)).isConnected).toBe(true);
    const facts = await screen.findByText(/Manual import/);
    expect(facts.textContent).toContain("bbbbbbbb…bbbb");
  });

  it("calls an admitted workflow startable and offers it no second admission", async () => {
    openCatalog({ ...listing([workflowSummary()]), ...admittedName() });

    expect((await screen.findByText(catalogPageCopy.startable)).isConnected).toBe(true);
    expect(screen.queryByRole("button", { name: catalogPageCopy.admit })).toBeNull();
  });

  it("marks an admitted name's unadmitted sibling as a newer revision instead of a second card", async () => {
    openCatalog({
      ...listing([
        workflowSummary({ workflow_revision_hash: WORKFLOW_HASH }),
        workflowSummary({ workflow_revision_hash: SIBLING_HASH })
      ]),
      ...admittedName()
    });

    expect(await screen.findAllByText(WORKFLOW_NAME)).toHaveLength(1);
    expect((await screen.findByText(catalogPageCopy.startable)).isConnected).toBe(true);
    expect(
      (await screen.findByText(catalogPageCopy.newerRevisionAvailable)).isConnected
    ).toBe(true);
  });

  it("carries no newer-revision note for a name with no sibling revision", async () => {
    openCatalog({ ...listing([workflowSummary()]), ...admittedName() });

    await screen.findByText(catalogPageCopy.startable);
    expect(screen.queryByText(catalogPageCopy.newerRevisionAvailable)).toBeNull();
  });

  it("links a startable entry's Start into the start room, never the reverse", async () => {
    openCatalog({ ...listing([workflowSummary()]), ...admittedName() });
    await screen.findByText(catalogPageCopy.startable);

    fireEvent.click(screen.getByRole("link", { name: catalogPageCopy.start }));

    expect((await screen.findByRole("heading", { name: "Workflows" })).isConnected).toBe(true);
  });

  it("offers admission for a published workflow the catalog does not hold yet", async () => {
    openCatalog({ ...listing([workflowSummary()]), ...unlistedName() });

    expect((await screen.findByText(catalogPageCopy.notAdmitted)).isConnected).toBe(true);
    expect(
      (await screen.findByRole("button", { name: catalogPageCopy.admit })).isConnected
    ).toBe(true);
  });

  it("makes a workflow startable through the admission door", async () => {
    let admitted = false;
    const foundCatalogLineage = vi.fn(async () => {
      admitted = true;
      return {
        status: 201,
        value: {
          display_name: WORKFLOW_NAME,
          lineage_id: LINEAGE_ID,
          workflow_revision_hash: WORKFLOW_HASH,
          revision_number: 1
        }
      };
    });
    openCatalog({
      ...listing([workflowSummary()]),
      getRevisionByName: vi.fn(async () => {
        if (!admitted) {
          throw new CockpitRequestError("no such name", {
            type: "urn:atelier2:problem:v1:catalog-name-not-found",
            title: "Catalog name not found",
            status: 404,
            detail: "no such name"
          } as Problem);
        }
        return {
          display_name: WORKFLOW_NAME,
          lineage_id: LINEAGE_ID,
          workflow_revision_hash: WORKFLOW_HASH,
          revision_number: 1
        };
      }),
      getWorkflowRevision: vi.fn(async () => ({
        workflow_revision_hash: WORKFLOW_HASH,
        document_base64: "YQ==",
        graph: {
          workflow_format_version: 3 as const,
          executable: true,
          not_executable_reason: null,
          node_count: 1,
          agent_roles: [],
          orders: [],
          wait_answer_schemas: [],
          node_previews: [],
          loops: [],
          name: WORKFLOW_NAME,
          description: null
        }
      })),
      foundCatalogLineage
    });

    await fireEvent.click(await screen.findByRole("button", { name: catalogPageCopy.admit }));

    expect((await screen.findByText(catalogPageCopy.startable)).isConnected).toBe(true);
    expect(foundCatalogLineage).toHaveBeenCalledOnce();
  });

  it("offers no admission for a workflow the admission door would refuse", async () => {
    openCatalog({
      ...listing([workflowSummary({ name: null })]),
      ...unlistedName()
    });

    expect(
      (await screen.findByText(catalogPageCopy.unnamedWorkflow)).isConnected
    ).toBe(true);
    expect(screen.queryByRole("button", { name: catalogPageCopy.admit })).toBeNull();
    // The catalog is asked by name, so a nameless revision gets no verdict at
    // all rather than one this room cannot stand behind.
    expect(screen.queryByText(catalogPageCopy.notAdmitted)).toBeNull();
    expect(screen.queryByText(catalogPageCopy.startable)).toBeNull();
  });

  it("names why a published workflow cannot run instead of offering admission", async () => {
    openCatalog({
      ...listing([
        workflowSummary({
          executable: false,
          not_executable_reason: "agent forms nothing binds yet: outputs"
        })
      ]),
      ...unlistedName()
    });

    expect(
      (await screen.findByText(catalogPageCopy.notExecutable)).isConnected
    ).toBe(true);
    expect(
      (await screen.findByText(/declares no output/)).isConnected
    ).toBe(true);
    expect(screen.queryByRole("button", { name: catalogPageCopy.admit })).toBeNull();
  });

  it("shows a published agent by name and says no executor runs it yet", async () => {
    openCatalog({
      listAgentDefinitionRevisions: vi.fn(async () => ({
        items: [agentItem()],
        next_after_revision_hash: null
      }))
    });

    expect((await screen.findByText("scribe")).isConnected).toBe(true);
    expect(
      (await screen.findByText(catalogPageCopy.agentPublishedOnly)).isConnected
    ).toBe(true);
  });

  it("marks which provider an imported agent belongs to", async () => {
    openCatalog({
      listAgentDefinitionRevisions: vi.fn(async () => ({
        items: [agentItem()],
        next_after_revision_hash: null
      }))
    });

    expect(
      (await screen.findByText(catalogPageCopy.agentProviderClaude)).isConnected
    ).toBe(true);
  });

  it("publishes pasted workflow bytes and lists what came back", async () => {
    const publish = vi.fn(async () => ({
      status: 201,
      value: {
        workflow_revision_hash: WORKFLOW_HASH,
        document_base64: "YQ==",
        graph: {
          workflow_format_version: 1 as const,
          start_node_id: "only",
          nodes: [
            {
              type: "action" as const,
              node_id: "only",
              next_node_id: "only"
            }
          ]
        }
      }
    }));
    let published = false;
    openCatalog({
      publish,
      listWorkflowRevisions: vi.fn(async () => ({
        items: published ? [workflowSummary()] : [],
        next_after_revision_hash: null
      })),
      ...unlistedName()
    });
    await screen.findByText(catalogPageCopy.workflowsEmpty);

    const field = screen.getByLabelText(catalogPageCopy.importWorkflowLabel);
    await fireEvent.input(field, { target: { value: EXACT_YAML } });
    published = true;
    await fireEvent.click(screen.getAllByRole("button", { name: catalogPageCopy.importAction })[0]!);

    expect((await screen.findByText(WORKFLOW_NAME)).isConnected).toBe(true);
    expect(publish).toHaveBeenCalledOnce();
  });

  it("publishes a pasted agent definition and lists what came back", async () => {
    const publishAgentDefinition = vi.fn(async () => ({
      status: 201,
      value: { agent_definition_revision_hash: AGENT_HASH }
    }));
    let published = false;
    openCatalog({
      publishAgentDefinition,
      listAgentDefinitionRevisions: vi.fn(async () => ({
        items: published ? [agentItem()] : [],
        next_after_revision_hash: null
      }))
    });
    await screen.findByText(catalogPageCopy.agentsEmpty);

    const field = screen.getByLabelText(catalogPageCopy.importAgentLabel);
    await fireEvent.input(field, { target: { value: EXACT_AGENT } });
    published = true;
    await fireEvent.click(screen.getAllByRole("button", { name: catalogPageCopy.importAction })[1]!);

    expect((await screen.findByText("scribe")).isConnected).toBe(true);
    expect(publishAgentDefinition).toHaveBeenCalledWith(EXACT_AGENT);
  });

  it("shows the refusal the API named rather than a sentence of its own", async () => {
    openCatalog({
      publishAgentDefinition: vi.fn(async () => {
        throw new CockpitRequestError("agent-definition-field-unknown: color", {
          type: "urn:atelier2:problem:v1:agent-definition-field-unknown",
          title: "Invalid agent definition document",
          status: 422,
          detail: "agent-definition-field-unknown: color"
        } as Problem);
      })
    });
    await screen.findByText(catalogPageCopy.agentsEmpty);

    const field = screen.getByLabelText(catalogPageCopy.importAgentLabel);
    await fireEvent.input(field, { target: { value: EXACT_AGENT } });
    await fireEvent.click(screen.getAllByRole("button", { name: catalogPageCopy.importAction })[1]!);

    expect(
      (await screen.findByText("agent-definition-field-unknown: color")).isConnected
    ).toBe(true);
    expect(
      (await screen.findByText("Invalid agent definition document")).isConnected
    ).toBe(true);
  });

  it("refuses to send an empty document instead of asking the API about nothing", async () => {
    const publishAgentDefinition = vi.fn();
    openCatalog({ publishAgentDefinition });
    await screen.findByText(catalogPageCopy.agentsEmpty);

    await fireEvent.click(screen.getAllByRole("button", { name: catalogPageCopy.importAction })[1]!);

    expect((await screen.findByText(catalogPageCopy.emptyDocument)).isConnected).toBe(true);
    expect(publishAgentDefinition).not.toHaveBeenCalled();
  });

  it("says the workflow list is unavailable rather than showing it as empty", async () => {
    openCatalog({
      listWorkflowRevisions: vi.fn(async () => {
        throw new CockpitRequestError("the store is asleep");
      })
    });

    await waitFor(() => {
      expect(screen.getByText(catalogPageCopy.workflowsUnavailable).isConnected).toBe(true);
    });
    expect(screen.queryByText(catalogPageCopy.workflowsEmpty)).toBeNull();
  });

  it("says the agent list is unavailable rather than showing it as empty", async () => {
    openCatalog({
      listAgentDefinitionRevisions: vi.fn(async () => {
        throw new CockpitRequestError("the store is asleep");
      })
    });

    await waitFor(() => {
      expect(screen.getByText(catalogPageCopy.agentsUnavailable).isConnected).toBe(true);
    });
    expect(screen.queryByText(catalogPageCopy.agentsEmpty)).toBeNull();
  });
});
