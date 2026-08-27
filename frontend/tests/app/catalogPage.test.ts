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
import { catalogPageCopy, workflowDetailCopy, workflowStartCopy } from "../../src/lib/catalogPageCopy";
import { shortFingerprint } from "../../src/lib/fingerprint";
import {
  reportConnectionLost,
  reportConnectionRestored,
  restartNoticeCopy
} from "../../src/lib/connectionState";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { cockpitApiStub } from "../support/cockpitApi";

const WORKFLOW_HASH = "b".repeat(64);
const SIBLING_HASH = "f".repeat(64);
const AGENT_HASH = "d".repeat(64);
const LINEAGE_ID = "e".repeat(64);
const WORKFLOW_NAME = "iterate-code";
const SECOND_WORKFLOW_NAME = "review-code";
const EXACT_YAML = "format_version: 3\nname: iterate-code\n";
const EXACT_AGENT = "---\nname: scribe\ndescription: Writes.\n---\n\nYou write.\n";

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
  reportConnectionRestored();
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

  it("leaves an admitted workflow calm and offers it no second admission", async () => {
    openCatalog({ ...listing([workflowSummary()]), ...admittedName() });

    const card = (await screen.findByText(WORKFLOW_NAME)).closest("li");
    expect(card).not.toBeNull();
    expect(screen.queryByRole("button", { name: catalogPageCopy.admit })).toBeNull();
  });

  it("carries a newer revision with one solid attention shape and a Why hint, not a state band", async () => {
    openCatalog({
      ...listing([
        workflowSummary({ workflow_revision_hash: WORKFLOW_HASH }),
        workflowSummary({ workflow_revision_hash: SIBLING_HASH })
      ]),
      ...admittedName()
    });

    expect(await screen.findAllByText(WORKFLOW_NAME)).toHaveLength(1);
    const card = screen.getByText(WORKFLOW_NAME).closest("li");
    expect(card).not.toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: catalogPageCopy.stateHint }));
    expect((await screen.findByText(catalogPageCopy.newerRevisionHint)).isConnected).toBe(true);
  });

  it("does not mark a workflow with no newer revision", async () => {
    openCatalog({ ...listing([workflowSummary()]), ...admittedName() });

    const card = (await screen.findByText(WORKFLOW_NAME)).closest("li");
    expect(card).not.toBeNull();
    expect(screen.queryByRole("button", { name: catalogPageCopy.stateHint })).toBeNull();
  });

  it("opens a named workflow's own detail room from its Details door, the only one it has", async () => {
    openCatalog({
      ...listing([workflowSummary()]),
      ...admittedName(),
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
      }))
    });
    await screen.findByText(WORKFLOW_NAME);

    fireEvent.click(screen.getByRole("link", { name: catalogPageCopy.details }));

    expect(
      (await screen.findByRole("heading", { name: WORKFLOW_NAME })).isConnected
    ).toBe(true);
  });

  it("proves(a-details-panel-shows-the-published-substance) proves(a-revision-hash-is-a-proof-anchor): names the published revision as proof and summarizes each declared order", async () => {
    const orderSchemaHash = "c".repeat(64);
    openCatalog({
      ...listing([workflowSummary()]),
      ...admittedName(),
      getWorkflowRevision: vi.fn(async () => ({
        workflow_revision_hash: WORKFLOW_HASH,
        document_base64: "YQ==",
        graph: {
          workflow_format_version: 3 as const,
          executable: true,
          not_executable_reason: null,
          node_count: 1,
          agent_roles: [],
          orders: [{ name: "portions", schema: { ref: "portions-schema", revision: orderSchemaHash } }],
          wait_answer_schemas: [],
          node_previews: [],
          loops: [],
          name: WORKFLOW_NAME,
          description: null
        }
      })),
      getSchemaRevision: vi.fn(async () => ({
        type: "object",
        properties: { portions: { type: "integer" } },
        required: ["portions"]
      }))
    });

    await screen.findByText(WORKFLOW_NAME);
    await fireEvent.click(screen.getByRole("link", { name: catalogPageCopy.details }));

    const revision = await screen.findByRole("group", { name: workflowDetailCopy.workflowRevision });
    expect(revision.textContent).toContain(shortFingerprint(WORKFLOW_HASH));
    expect(revision.textContent).toContain(workflowDetailCopy.sealsWorkflowRevision);
    expect((await screen.findByRole("heading", { name: workflowDetailCopy.orders })).isConnected).toBe(true);
    expect(screen.getByText(/portions-schema/).isConnected).toBe(true);
    expect(screen.getByText(/portions · integer · required/).isConnected).toBe(true);
  });

  it("opens the selected workflow's start sheet instead of leaving the catalog detail", async () => {
    openCatalog({
      ...listing([workflowSummary()]),
      ...admittedName(),
      getWorkflowRevision: vi.fn(async () => ({
        workflow_revision_hash: WORKFLOW_HASH,
        document_base64: "YQ==",
        graph: {
          workflow_format_version: 3 as const,
          executable: true,
          not_executable_reason: null,
          node_count: 1,
          agent_roles: ["builder"],
          orders: [],
          wait_answer_schemas: [],
          node_previews: [],
          loops: [],
          name: WORKFLOW_NAME,
          description: null
        }
      })),
      listAgentConfigurationRevisions: vi.fn(async () => ({
        items: [
          {
            agent_configuration_revision_hash: AGENT_HASH,
            provider_id: "claude",
            model: "test-model",
            auth_mode: "subscription" as const,
            auth_profile_revision_hash: "a".repeat(64),
            executor_revision: "test/v1",
            requested_capability: "headless" as const,
            startable: true,
            not_startable_reason: null
          }
        ],
        next_after_revision_hash: null
      })),
      listAuthProfileRevisions: vi.fn(async () => ({
        items: [{
          profile_id: "operator",
          revision_number: 1,
          provider_id: "claude",
          auth_mode: "subscription" as const,
          auth_profile_revision_hash: "a".repeat(64)
        }],
        next_after_revision_hash: null
      })),
      getModelRegistry: vi.fn(async () => ({
        provider_id: "claude",
        revision_number: 1,
        model_registry_revision_hash: "9".repeat(64),
        entries: [{
          model_id: "test-model",
          agent_configuration_revision_hash: AGENT_HASH,
          source: "discovered" as const,
          provider_check: "checked" as const
        }]
      })),
      listProjects: vi.fn(async () => ({
        items: [{ public_project_reference: "project1.YXRlbGllcg" }]
      })),
      resolveProjectModels: vi.fn(async (_project, workflowRevisionHash) => ({
        project_id: "atelier",
        public_project_reference: "project1.YXRlbGllcg",
        workflow_revision_hash: workflowRevisionHash,
        resolutions: [{
          role: "builder",
          agent_configuration_revision_hash: null,
          source: "uncast" as const,
          model_id: null,
          declared_difficulty: 2 as const,
          default_difficulty: null,
          uncast_reason: "no-project-default" as const,
          family_differs_from: null
        }]
      }))
    });
    await screen.findByText(WORKFLOW_NAME);
    await fireEvent.click(screen.getByRole("link", { name: catalogPageCopy.details }));

    await fireEvent.click(await screen.findByRole("button", { name: catalogPageCopy.start }));

    expect((await screen.findByRole("heading", { name: `Start ${WORKFLOW_NAME}` })).isConnected).toBe(
      true
    );
    expect(await screen.findByLabelText("Configuration for builder")).toBeTruthy();
    const startRun = screen.getByRole("button", { name: workflowStartCopy.startRun }) as HTMLButtonElement;
    expect(startRun.disabled).toBe(true);
    expect(startRun.title).toBe(workflowStartCopy.startNeedsConfiguration("builder"));
    expect(screen.queryByRole("button", { name: /Why builder/ })).toBeNull();
  });

  it("offers no Details door for a revision that declares no name to look one up by", async () => {
    openCatalog({ ...listing([workflowSummary({ name: null })]), ...unlistedName() });
    await screen.findByText(catalogPageCopy.unnamedWorkflow);

    expect(screen.queryByRole("link", { name: catalogPageCopy.details })).toBeNull();
  });

  it("proves(an-unadmitted-or-uncatalogable-published-name-is-named-in-the-picker): offers admission for a published workflow the catalog does not hold yet", async () => {
    openCatalog({ ...listing([workflowSummary()]), ...unlistedName() });

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

    expect((await screen.findByText(WORKFLOW_NAME)).isConnected).toBe(true);
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
    expect(screen.queryByRole("button", { name: catalogPageCopy.stateHint })).toBeNull();
  });

  it("proves(a-revision-no-run-can-start-says-so-before-the-operator-tries) proves(a-revision-no-run-can-start-says-so-where-it-was-published): carries a blocked workflow with a distinct shape and names why on ask", async () => {
    openCatalog({
      ...listing([
        workflowSummary({
          executable: false,
          not_executable_reason: "agent forms nothing binds yet: outputs"
        })
      ]),
      ...unlistedName()
    });

    const card = (await screen.findByText(WORKFLOW_NAME)).closest("li");
    expect(card).not.toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: catalogPageCopy.stateHint }));
    expect((await screen.findByText(
      "This workflow declares no output. Add one outputs: entry on the agent node and publish again."
    )).isConnected).toBe(true);
    expect(screen.queryByRole("button", { name: catalogPageCopy.admit })).toBeNull();
  });

  it("proves(the-picker-offers-every-saved-workflow-not-only-its-first-page): lists a workflow that arrives only after the first page", async () => {
    openCatalog({
      listWorkflowRevisions: vi.fn(async (after?: string) =>
        after === undefined
          ? { items: [workflowSummary()], next_after_revision_hash: WORKFLOW_HASH }
          : {
              items: [
                workflowSummary({
                  workflow_revision_hash: SIBLING_HASH,
                  name: SECOND_WORKFLOW_NAME
                })
              ],
              next_after_revision_hash: null
            }
      ),
      getRevisionByName: vi.fn(async (name: string) => ({
        display_name: name,
        lineage_id: LINEAGE_ID,
        workflow_revision_hash: name === WORKFLOW_NAME ? WORKFLOW_HASH : SIBLING_HASH,
        revision_number: 1
      }))
    });

    expect((await screen.findByText(SECOND_WORKFLOW_NAME)).isConnected).toBe(true);
  });

  it("carries a published agent with the dashed blocked shape and says why on ask", async () => {
    openCatalog({
      listAgentDefinitionRevisions: vi.fn(async () => ({
        items: [agentItem()],
        next_after_revision_hash: null
      }))
    });

    expect((await screen.findByText("scribe")).isConnected).toBe(true);
    const card = screen.getByText("scribe").closest("li");
    expect(card).not.toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: catalogPageCopy.stateHint }));
    expect((await screen.findByText(catalogPageCopy.agentUnavailableHint)).isConnected).toBe(true);
    expect(screen.getByText(catalogPageCopy.agentUnavailableHint).closest("code")).toBeNull();
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

  it("names no local failure while the whole workshop reads unreachable, and reads itself again once the connection returns (#700)", async () => {
    const listWorkflowRevisions = vi.fn().mockRejectedValue(new Error("Failed to fetch"));
    const listAgentDefinitionRevisions = vi.fn().mockRejectedValue(new Error("Failed to fetch"));
    openCatalog({ listWorkflowRevisions, listAgentDefinitionRevisions });
    await screen.findByRole("heading", { name: "Catalog" });

    reportConnectionLost();
    await waitFor(() => {
      expect(document.querySelector(".notice-banner")?.textContent).toContain(restartNoticeCopy);
    });
    // The shell's one line above already names the outage; this room adds no
    // second, page-local echo of the same fact for either list.
    expect(screen.queryByText(catalogPageCopy.workflowsUnavailable)).toBeNull();
    expect(screen.queryByText(catalogPageCopy.agentsUnavailable)).toBeNull();

    listWorkflowRevisions.mockResolvedValue({ items: [], next_after_revision_hash: null });
    listAgentDefinitionRevisions.mockResolvedValue({ items: [], next_after_revision_hash: null });
    reportConnectionRestored();

    expect((await screen.findByText(catalogPageCopy.workflowsEmpty)).isConnected).toBe(true);
    expect(screen.getByText(catalogPageCopy.agentsEmpty).isConnected).toBe(true);
    expect(screen.queryByText(catalogPageCopy.workflowsUnavailable)).toBeNull();
    expect(screen.queryByText(catalogPageCopy.agentsUnavailable)).toBeNull();
  });
});
