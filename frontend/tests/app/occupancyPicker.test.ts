import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  CockpitRequestError,
  type AgentConfigurationRevisionListItem,
  type CockpitApi,
  type Problem,
  type WorkflowRevisionDetail,
  type WorkflowRevisionSummary
} from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { NAMED_AGENT_CHOICE_STORAGE_KEY } from "../../src/lib/namedAgentChoice";
import { cockpitApiStub } from "../support/cockpitApi";

const workflowHash = "1".repeat(64);
const lineageId = "2".repeat(64);
const projectReference = "project1.dGVhbS9yZWQ";
const projectHash = "3".repeat(64);
const rememberedHash = "4".repeat(64);
const otherHash = "5".repeat(64);
const unknownHash = "6".repeat(64);
const workflowName = "project-occupancy-proof";

function problem(
  code:
    | "occupancy-missing"
    | "project-unknown"
    | "catalog-name-not-found"
    | "catalog-lineage-retired"
): Problem {
  return {
    type: `urn:atelier2:problem:v1:${code}`,
    title: code === "occupancy-missing" ? "Occupancy not found" : "Project unknown",
    status: 404,
    detail: code
  } as Problem;
}

function summary(
  hash = workflowHash,
  name = workflowName
): WorkflowRevisionSummary {
  return {
    workflow_revision_hash: hash,
    workflow_format_version: 3,
    executable: true,
    not_executable_reason: null,
    name,
    description: "Resolve each role from its honest owner."
  };
}

function detail(
  hash = workflowHash,
  name = workflowName,
  roles = ["builder", "reviewer", "auditor"]
): WorkflowRevisionDetail {
  return {
    workflow_revision_hash: hash,
    document_base64: "YQ==",
    graph: {
      workflow_format_version: 3,
      executable: true,
      not_executable_reason: null,
      node_count: roles.length,
      agent_roles: roles,
      orders: [],
      node_previews: roles.map((role, index) => ({
        id: `node-${index}`,
        kind: "agent" as const,
        role,
        instruction_start: `Work as ${role}.`,
        depends_on: index === 0 ? [] : [`node-${index - 1}`]
      })),
      loops: [],
      name,
      description: "Resolve each role from its honest owner."
    }
  };
}

function agent(
  hash: string,
  provider: string,
  model: string,
  startable = true
): AgentConfigurationRevisionListItem {
  return {
    model,
    auth_profile_revision_hash: "a".repeat(64),
    executor_revision: `${provider}/v1`,
    provider_id: provider,
    auth_mode: "subscription",
    requested_capability: "headless",
    agent_configuration_revision_hash: hash,
    startable,
    not_startable_reason: startable ? null : "agent-executor-binding-unavailable"
  };
}

function occupancy(
  bindings: Array<{ role: string; agent_configuration_revision_hash: string }>,
  activeLineage = lineageId
) {
  return {
    project_id: "team/red",
    public_project_reference: projectReference,
    lineage_id: activeLineage,
    revision_number: 1,
    occupancy_revision_hash: "b".repeat(64),
    bindings
  };
}

function pickerApi(
  getProjectOccupancy: ReturnType<typeof vi.fn>,
  overrides: Partial<CockpitApi> = {}
): CockpitApi {
  const occupancyPort = { getProjectOccupancy } as unknown as Partial<CockpitApi>;
  return cockpitApiStub({
    listWorkflowRevisions: vi.fn(async () => ({
      items: [summary()],
      next_after_revision_hash: null
    })),
    getRevisionByName: vi.fn(async () => ({
      display_name: workflowName,
      lineage_id: lineageId,
      workflow_revision_hash: workflowHash,
      revision_number: 1
    })),
    getWorkflowRevision: vi.fn(async () => detail()),
    listAgentConfigurationRevisions: vi.fn(async () => ({
      items: [
        agent(projectHash, "project-provider", "project-model"),
        agent(rememberedHash, "remembered-provider", "remembered-model"),
        agent(otherHash, "other-provider", "other-model")
      ],
      next_after_revision_hash: null
    })),
    listProjects: vi.fn(async () => ({
      items: [{ public_project_reference: projectReference }]
    })),
    ...occupancyPort,
    ...overrides
  });
}

async function openDraft(
  cockpitApi: CockpitApi,
  name = workflowName,
  firstRole = "builder"
): Promise<void> {
  render(App, {
    props: {
      cockpitApi,
      mutationJournal: new MutationJournal(sessionStorage),
      createRunId: () => "occupancy-run"
    }
  });
  await fireEvent.click(await screen.findByRole("radio", { name: new RegExp(name) }));
  await screen.findByRole("article", { name: `Binding ${firstRole}` });
}

function binding(role: string): HTMLElement {
  return screen.getByRole("article", { name: `Binding ${role}` });
}

function picker(role: string): HTMLSelectElement {
  return within(binding(role)).getByLabelText(`Agent for ${role}`) as HTMLSelectElement;
}

function source(role: string, label: string): HTMLElement {
  return within(binding(role)).getByLabelText(`Binding source: ${label}`);
}

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  window.history.replaceState(null, "", "/atelier/new");
});

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
});

describe("project occupancy in the New Run picker", () => {
  it("uses project, then remembered, then empty independently and keeps Head C's lineage", async () => {
    localStorage.setItem(
      NAMED_AGENT_CHOICE_STORAGE_KEY,
      JSON.stringify({ builder: rememberedHash, reviewer: rememberedHash })
    );
    const getProjectOccupancy = vi.fn(async () =>
      occupancy([
        { role: "builder", agent_configuration_revision_hash: projectHash },
        { role: "foreign", agent_configuration_revision_hash: otherHash }
      ])
    );
    const start = vi.fn(async (mutation: Parameters<CockpitApi["start"]>[0]) => {
      void mutation;
      return await new Promise<never>(() => {});
    });
    const cockpitApi = pickerApi(getProjectOccupancy, { start });

    await openDraft(cockpitApi);

    await waitFor(() => expect(picker("builder").value).toBe(projectHash));
    expect(source("builder", "Project").isConnected).toBe(true);
    expect(picker("reviewer").value).toBe(rememberedHash);
    expect(source("reviewer", "Remembered").isConnected).toBe(true);
    expect(picker("auditor").value).toBe("");
    expect(source("auditor", "Choose").isConnected).toBe(true);
    expect(screen.queryByRole("article", { name: "Binding foreign" })).toBeNull();
    expect(getProjectOccupancy).toHaveBeenCalledWith(projectReference, lineageId);
    expect(cockpitApi.getRevisionByName).toHaveBeenCalledTimes(1);

    await fireEvent.change(picker("auditor"), { target: { value: otherHash } });
    await fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(start).toHaveBeenCalledTimes(1));
    const body = JSON.parse(globalThis.atob(start.mock.calls[0]?.[0].body_base64 ?? ""));
    expect(body.agent_bindings).toEqual([
      { role: "builder", agent_configuration_revision_hash: projectHash },
      { role: "reviewer", agent_configuration_revision_hash: rememberedHash },
      { role: "auditor", agent_configuration_revision_hash: otherHash }
    ]);
  });

  it("proves(a-listed-agent-configuration-names-current-startability): keeps a listed unavailable project binding selected until the operator switches it", async () => {
    const cockpitApi = pickerApi(
      vi.fn(async () =>
        occupancy([{ role: "builder", agent_configuration_revision_hash: projectHash }])
      ),
      {
        listAgentConfigurationRevisions: vi.fn(async () => ({
          items: [
            agent(projectHash, "project-provider", "project-model", false),
            agent(otherHash, "other-provider", "other-model")
          ],
          next_after_revision_hash: null
        }))
      }
    );

    await openDraft(cockpitApi);

    const selected = picker("builder");
    expect(selected.value).toBe(projectHash);
    expect(source("builder", "Project").isConnected).toBe(true);
    expect(within(binding("builder")).getByText("Unavailable").isConnected).toBe(true);
    expect(
      (within(binding("builder")).getByRole("option", {
        name: /project-model.*Unavailable/
      }) as HTMLOptionElement).disabled
    ).toBe(true);
    expect(screen.queryByRole("button", { name: "Start" })).toBeNull();

    await fireEvent.change(selected, { target: { value: otherHash } });

    expect(selected.value).toBe(otherHash);
    expect(source("builder", "Remembered").isConnected).toBe(true);
    expect(screen.getByRole("button", { name: "Start" }).isConnected).toBe(true);
    expect(cockpitApi.putProjectOccupancy).not.toHaveBeenCalled();
  });

  it("does not turn an unknown project binding into a remembered choice", async () => {
    localStorage.setItem(
      NAMED_AGENT_CHOICE_STORAGE_KEY,
      JSON.stringify({ builder: rememberedHash })
    );
    const cockpitApi = pickerApi(
      vi.fn(async () =>
        occupancy([{ role: "builder", agent_configuration_revision_hash: unknownHash }])
      )
    );

    await openDraft(cockpitApi);

    await waitFor(() => expect(source("builder", "Unavailable")).toBeTruthy());
    expect(picker("builder").value).toBe(unknownHash);
    expect(picker("builder").textContent).toContain("Unavailable");
    expect(picker("builder").value).not.toBe(rememberedHash);

    await fireEvent.change(picker("builder"), { target: { value: rememberedHash } });

    expect(picker("builder").value).toBe(rememberedHash);
    expect(source("builder", "Remembered").isConnected).toBe(true);
    expect(localStorage.getItem(NAMED_AGENT_CHOICE_STORAGE_KEY)).toContain(rememberedHash);
  });

  it("keeps a manual choice when the project answer arrives late", async () => {
    let releaseOccupancy!: (value: ReturnType<typeof occupancy>) => void;
    const delayed = new Promise<ReturnType<typeof occupancy>>((resolve) => {
      releaseOccupancy = resolve;
    });
    const cockpitApi = pickerApi(vi.fn(async () => delayed));

    await openDraft(cockpitApi);

    expect(source("builder", "Looking…").isConnected).toBe(true);
    await fireEvent.change(picker("builder"), { target: { value: rememberedHash } });
    releaseOccupancy(
      occupancy([{ role: "builder", agent_configuration_revision_hash: projectHash }])
    );

    await waitFor(() => expect(screen.queryByText("Looking…")).toBeNull());
    expect(picker("builder").value).toBe(rememberedHash);
    expect(source("builder", "Remembered").isConnected).toBe(true);
  });

  it("waits for a complete agent list before proving a project hash unavailable", async () => {
    let releaseAgents!: (value: {
      items: AgentConfigurationRevisionListItem[];
      next_after_revision_hash: null;
    }) => void;
    const delayedAgents = new Promise<{
      items: AgentConfigurationRevisionListItem[];
      next_after_revision_hash: null;
    }>((resolve) => {
      releaseAgents = resolve;
    });
    const cockpitApi = pickerApi(
      vi.fn(async () =>
        occupancy([{ role: "builder", agent_configuration_revision_hash: unknownHash }])
      ),
      { listAgentConfigurationRevisions: vi.fn(async () => delayedAgents) }
    );

    await openDraft(cockpitApi);

    expect(source("builder", "Looking…").isConnected).toBe(true);
    expect(within(binding("builder")).queryByLabelText("Binding source: Unavailable")).toBeNull();
    releaseAgents({ items: [], next_after_revision_hash: null });

    await waitFor(() =>
      expect(source("builder", "Unavailable").isConnected).toBe(true)
    );
  });

  it("treats missing occupancy as absence but retries an unavailable read", async () => {
    localStorage.setItem(
      NAMED_AGENT_CHOICE_STORAGE_KEY,
      JSON.stringify({ builder: rememberedHash })
    );
    const missing = pickerApi(
      vi.fn(async () => {
        throw new CockpitRequestError(
          "missing",
          problem("occupancy-missing"),
          true
        );
      })
    );
    await openDraft(missing);

    await waitFor(() => expect(picker("builder").value).toBe(rememberedHash));
    expect(screen.queryByText("Project occupancy unavailable")).toBeNull();
    cleanup();

    const getProjectOccupancy = vi
      .fn()
      .mockRejectedValueOnce(new Error("private transport detail"))
      .mockResolvedValueOnce(
        occupancy([{ role: "builder", agent_configuration_revision_hash: projectHash }])
      );
    const unavailable = pickerApi(getProjectOccupancy);
    await openDraft(unavailable);

    await screen.findByText("Project occupancy unavailable");
    expect(screen.queryByText(/private transport detail/)).toBeNull();
    const retry = screen.getByRole("button", { name: "Retry project occupancy" });
    expect(screen.getAllByRole("button", { name: "Retry project occupancy" })).toHaveLength(1);

    await fireEvent.click(retry);

    await waitFor(() => expect(picker("builder").value).toBe(projectHash));
    expect(getProjectOccupancy).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("Project occupancy unavailable")).toBeNull();
  });

  it("retains confirmed same-lineage truth when a reopened draft refresh fails", async () => {
    localStorage.setItem(
      NAMED_AGENT_CHOICE_STORAGE_KEY,
      JSON.stringify({ builder: rememberedHash })
    );
    const getProjectOccupancy = vi
      .fn()
      .mockResolvedValueOnce(
        occupancy([{ role: "builder", agent_configuration_revision_hash: projectHash }])
      )
      .mockRejectedValueOnce(new Error("private refresh detail"))
      .mockResolvedValueOnce(
        occupancy([{ role: "builder", agent_configuration_revision_hash: projectHash }])
      );
    const cockpitApi = pickerApi(getProjectOccupancy);

    await openDraft(cockpitApi);
    await waitFor(() => expect(source("builder", "Project")).toBeTruthy());
    await fireEvent.click(screen.getByRole("button", { name: "Change" }));
    await fireEvent.click(screen.getByRole("radio", { name: new RegExp(workflowName) }));

    await screen.findByText("Project occupancy unavailable");
    expect(picker("builder").value).toBe(projectHash);
    expect(source("builder", "Project").isConnected).toBe(true);
    expect(screen.queryByText(/private refresh detail/)).toBeNull();
    expect(screen.getAllByRole("button", { name: "Retry project occupancy" })).toHaveLength(1);

    await fireEvent.click(screen.getByRole("button", { name: "Retry project occupancy" }));
    await waitFor(() => expect(screen.queryByText("Project occupancy unavailable")).toBeNull());
    expect(getProjectOccupancy).toHaveBeenCalledTimes(3);
    expect(picker("builder").value).toBe(projectHash);
  });

  it("rejects a late result from the draft before the current lineage", async () => {
    const secondHash = "7".repeat(64);
    const secondLineage = "8".repeat(64);
    const secondName = "second-occupancy-proof";
    let releaseFirst!: (value: ReturnType<typeof occupancy>) => void;
    const firstRead = new Promise<ReturnType<typeof occupancy>>((resolve) => {
      releaseFirst = resolve;
    });
    const getProjectOccupancy = vi.fn(async (_project: string, askedLineage: string) =>
      askedLineage === lineageId
        ? firstRead
        : occupancy(
            [{ role: "builder", agent_configuration_revision_hash: otherHash }],
            secondLineage
          )
    );
    const cockpitApi = pickerApi(getProjectOccupancy, {
      listWorkflowRevisions: vi.fn(async () => ({
        items: [summary(), summary(secondHash, secondName)],
        next_after_revision_hash: null
      })),
      getRevisionByName: vi.fn(async (name: string) => ({
        display_name: name,
        lineage_id: name === workflowName ? lineageId : secondLineage,
        workflow_revision_hash: name === workflowName ? workflowHash : secondHash,
        revision_number: 1
      })),
      getWorkflowRevision: vi.fn(async (hash: string) =>
        hash === workflowHash ? detail() : detail(secondHash, secondName, ["builder"])
      )
    });

    await openDraft(cockpitApi);
    expect(source("builder", "Looking…").isConnected).toBe(true);
    await fireEvent.click(screen.getByRole("button", { name: "Change" }));
    await fireEvent.click(screen.getByRole("radio", { name: new RegExp(secondName) }));
    await waitFor(() => expect(picker("builder").value).toBe(otherHash));

    releaseFirst(
      occupancy([{ role: "builder", agent_configuration_revision_hash: projectHash }])
    );
    await Promise.resolve();
    expect(picker("builder").value).toBe(otherHash);
    expect(source("builder", "Project").isConnected).toBe(true);
  });

  it("replaces an in-flight occupancy read for a newer draft of the same lineage", async () => {
    const secondHash = "7".repeat(64);
    let releaseFirst!: (value: ReturnType<typeof occupancy>) => void;
    const firstRead = new Promise<ReturnType<typeof occupancy>>((resolve) => {
      releaseFirst = resolve;
    });
    const getProjectOccupancy = vi
      .fn()
      .mockImplementationOnce(async () => firstRead)
      .mockResolvedValueOnce(
        occupancy([{ role: "builder", agent_configuration_revision_hash: otherHash }])
      );
    const cockpitApi = pickerApi(getProjectOccupancy, {
      listWorkflowRevisions: vi.fn(async () => ({
        items: [summary(), summary(secondHash)],
        next_after_revision_hash: null
      })),
      getWorkflowRevision: vi.fn(async (hash: string) =>
        hash === workflowHash ? detail() : detail(secondHash)
      )
    });

    await openDraft(cockpitApi);
    expect(source("builder", "Looking…").isConnected).toBe(true);
    await fireEvent.change(screen.getByLabelText(`Revision of ${workflowName}`), {
      target: { value: secondHash }
    });

    await waitFor(() => expect(getProjectOccupancy).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(picker("builder").value).toBe(otherHash));
    releaseFirst(
      occupancy([{ role: "builder", agent_configuration_revision_hash: projectHash }])
    );
    await Promise.resolve();
    expect(picker("builder").value).toBe(otherHash);
    expect(source("builder", "Project").isConnected).toBe(true);
  });

  it("treats every role as own data across project, remembered, empty and persistence", async () => {
    localStorage.setItem(
      NAMED_AGENT_CHOICE_STORAGE_KEY,
      JSON.stringify(Object.fromEntries([["constructor", rememberedHash]]))
    );
    const cockpitApi = pickerApi(
      vi.fn(async () =>
        occupancy([{ role: "__proto__", agent_configuration_revision_hash: projectHash }])
      ),
      {
        getWorkflowRevision: vi.fn(async () =>
          detail(workflowHash, workflowName, ["constructor", "__proto__", "toString"])
        )
      }
    );

    await openDraft(cockpitApi, workflowName, "constructor");

    await waitFor(() => expect(source("__proto__", "Project")).toBeTruthy());
    expect(picker("constructor").value).toBe(rememberedHash);
    expect(source("constructor", "Remembered").isConnected).toBe(true);
    expect(picker("toString").value).toBe("");
    expect(source("toString", "Choose").isConnected).toBe(true);

    await fireEvent.change(picker("__proto__"), { target: { value: otherHash } });

    const stored = JSON.parse(
      localStorage.getItem(NAMED_AGENT_CHOICE_STORAGE_KEY) ?? "{}"
    ) as Record<string, unknown>;
    expect(Object.hasOwn(stored, "__proto__")).toBe(true);
    expect(stored["__proto__"]).toBe(otherHash);
  });

  it("never reads occupancy for an unlisted, retired, unnamable or roleless workflow", async () => {
    const rolelessHash = "7".repeat(64);
    const rolelessLineage = "8".repeat(64);
    const rolelessName = "roleless-proof";
    const retiredHash = "9".repeat(64);
    const retiredName = "retired-proof";
    const unnamableHash = "a".repeat(64);
    const unnamableName = "Unnamable title";
    localStorage.setItem(
      NAMED_AGENT_CHOICE_STORAGE_KEY,
      JSON.stringify({ builder: rememberedHash })
    );
    const cockpitApi = pickerApi(vi.fn(), {
      listWorkflowRevisions: vi.fn(async () => ({
        items: [
          summary(),
          summary(retiredHash, retiredName),
          summary(unnamableHash, unnamableName),
          summary(rolelessHash, rolelessName)
        ],
        next_after_revision_hash: null
      })),
      getRevisionByName: vi.fn(async (name: string) => {
        if (name === workflowName || name === retiredName) {
          throw new CockpitRequestError(
            name === workflowName ? "unlisted" : "retired",
            problem(
              name === workflowName
                ? "catalog-name-not-found"
                : "catalog-lineage-retired"
            ),
            true
          );
        }
        return {
          display_name: rolelessName,
          lineage_id: rolelessLineage,
          workflow_revision_hash: rolelessHash,
          revision_number: 1
        };
      }),
      getWorkflowRevision: vi.fn(async (hash: string) =>
        hash === rolelessHash
          ? detail(rolelessHash, rolelessName, [])
          : hash === retiredHash
            ? detail(retiredHash, retiredName, ["builder"])
            : hash === unnamableHash
              ? detail(unnamableHash, unnamableName, ["builder"])
              : detail()
      )
    });

    await openDraft(cockpitApi);
    for (const name of [workflowName, retiredName, unnamableName]) {
      await waitFor(() => expect(picker("builder").value).toBe(rememberedHash));
      expect(source("builder", "Remembered").isConnected).toBe(true);
      await fireEvent.click(screen.getByRole("button", { name: "Change" }));
      if (name !== unnamableName) {
        const nextName = name === workflowName ? retiredName : unnamableName;
        await fireEvent.click(screen.getByRole("radio", { name: new RegExp(nextName) }));
      }
    }
    await fireEvent.click(screen.getByRole("radio", { name: new RegExp(rolelessName) }));
    await waitFor(() =>
      expect(screen.queryByRole("article", { name: "Binding builder" })).toBeNull()
    );
    expect(cockpitApi.listProjects).not.toHaveBeenCalled();
    expect(cockpitApi.getProjectOccupancy).not.toHaveBeenCalled();
  });
});
