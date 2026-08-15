import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  CockpitRequestError,
  createCockpitApi,
  type CockpitApi,
  type RunEventHandlers,
  type RunV1,
  type RunV2,
  type WorkflowRevisionDetail
} from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";

const revisionHash = "a".repeat(64);
const publicReference = "run1.cnVuLWRyYWZ0";
const v2PublicReference = "run1.cnVuLXYy";

beforeEach(() => {
  sessionStorage.clear();
  window.history.replaceState(null, "", "/atelier/runs");
});

afterEach(() => cleanup());

describe("Phase 2 mobile run entry", () => {
  it("lists a bounded durable run page and keeps confirmed rows visible when refresh fails", async () => {
    const listRuns = vi.fn().mockResolvedValue({ items: [run()], next_after: null });
    const cockpitApi = api({ listRuns });
    render(App, { props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) } });

    expect((await screen.findByRole("link", { name: /run-draft/i })).getAttribute("href")).toBe(
      `/atelier/runs/${publicReference}`
    );
    listRuns.mockRejectedValueOnce(new Error("offline"));
    await fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    expect((await screen.findByRole("alert")).textContent).toContain("offline");
    expect(screen.getByRole("link", { name: /run-draft/i }).isConnected).toBe(true);
  });

  it("new_saved_mobile starts a saved revision with one visible Run ID and stable bytes", async () => {
    window.history.replaceState(null, "", "/atelier/new");
    const cockpitApi = api();
    const createRunId = vi.fn(() => "run-draft");
    render(App, {
      props: {
        cockpitApi,
        mutationJournal: new MutationJournal(sessionStorage),
        createRunId
      }
    });

    await fireEvent.click(await screen.findByLabelText(revisionHash));
    expect(screen.getByText("run-draft").isConnected).toBe(true);
    await fireEvent.click(screen.getByRole("button", { name: "Start" }));

    await waitFor(() => expect(window.location.pathname).toBe(`/atelier/runs/${publicReference}`));
    const mutation = vi.mocked(cockpitApi.start).mock.calls[0]?.[0];
    expect(jsonBody(mutation)).toEqual({ run_id: "run-draft", workflow_revision_hash: revisionHash });
    expect(createRunId).toHaveBeenCalledTimes(1);
  });

  it("new_publish_mobile confirms before sending exact YAML and then offers Start", async () => {
    window.history.replaceState(null, "", "/atelier/new");
    const cockpitApi = api({
      publish: vi.fn(async (mutation) => ({
        status: 201,
        value: {
          revision_hash: mutation.mutation_id.slice("publish:".length),
          document_base64: mutation.body_base64,
          graph: graph()
        }
      }))
    });
    render(App, {
      props: {
        cockpitApi,
        mutationJournal: new MutationJournal(sessionStorage),
        createRunId: () => "run-published"
      }
    });
    const exactYaml = "format_version: 1\nstart_node_id: agent\nlabel: Grüße 東京\n";

    await fireEvent.click(await screen.findByLabelText("Publish YAML"));
    await fireEvent.input(screen.getByLabelText("Exact workflow YAML"), {
      target: { value: exactYaml }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Review publication" }));
    expect(cockpitApi.publish).not.toHaveBeenCalled();
    const dialog = screen.getByRole("dialog", { name: "Publish this exact workflow?" });
    await fireEvent.click(withinRole(dialog, "button", "Publish"));

    await waitFor(() => expect(cockpitApi.publish).toHaveBeenCalledTimes(1));
    const mutation = vi.mocked(cockpitApi.publish).mock.calls[0]?.[0];
    expect(textBody(mutation)).toBe(exactYaml);
    expect((await screen.findByText("run-published")).isConnected).toBe(true);
    expect(screen.getByRole("button", { name: "Start" }).isConnected).toBe(true);
  });

  it("publishes each distinct V2 role binding and starts its exact request", async () => {
    window.history.replaceState(null, "", "/atelier/new");
    const authHash = "b".repeat(64);
    let publishedRevision: WorkflowRevisionDetail;
    let boundRun: RunV2;
    const eventFeed: { handlers: RunEventHandlers | null } = { handlers: null };
    let startResponses = 0, continueRetry = (): void => {};
    const retryGate = new Promise<void>((resolve) => { continueRetry = resolve; });
    let rejectConfiguration = (reason: unknown): void => void reason;
    const configurationFailure = new Promise<never>((_, reject) => { rejectConfiguration = reject; });
    const cockpitApi = api({
      publish: vi.fn(async (mutation) => {
        publishedRevision = v2Revision(mutation.mutation_id.slice("publish:".length), mutation.body_base64);
        return { status: 201, value: publishedRevision };
      }),
      publishAuthProfile: vi.fn(async (input) => ({
        status: 201,
        value: { ...input, auth_profile_revision_hash: authHash }
      })),
      publishAgentConfiguration: vi.fn(async (input) => ({
        status: 201,
        value: {
          ...input,
          provider_id: input.model === "sonnet" ? "anthropic" : "openai",
          auth_mode: input.model === "sonnet" ? "subscription" as const : "api_key" as const,
          agent_configuration_revision_hash: input.model === "sonnet" ? "c".repeat(64) : "d".repeat(64)
        }
      })).mockReturnValueOnce(configurationFailure),
      start: vi.fn(async (mutation) => {
        if (++startResponses === 2) await retryGate;
        return { status: 201, value: startResponses < 3
          ? v2Run(jsonBody(mutation), []) : (boundRun = v2Run(jsonBody(mutation), v2Bindings(authHash))) };
      }),
      getRun: vi.fn(async () => boundRun),
      getWorkflowRevision: vi.fn(async () => publishedRevision),
      openRunEvents: vi.fn((_publicReference, handlers) => {
        eventFeed.handlers = handlers;
        return { close: vi.fn() };
      })
    });
    render(App, {
      props: {
        cockpitApi,
        mutationJournal: new MutationJournal(sessionStorage),
        createRunId: () => "run-v2"
      }
    });

    await fireEvent.click(await screen.findByLabelText("Publish YAML"));
    await fireEvent.input(screen.getByLabelText("Exact workflow YAML"), {
      target: { value: "format_version: 2\nstart: build\n" }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Review publication" }));
    await fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    expect(await screen.findAllByRole("article", { name: /^Binding / })).toHaveLength(2);
    expect(screen.getAllByText("builder", { selector: "h3" })).toHaveLength(1);

    await fireEvent.click(screen.getByRole("button", { name: "Start" }));
    expect(screen.getAllByText("Complete every field.")).toHaveLength(2);
    expect(screen.getByRole("article", { name: "Binding builder" }).classList).toContain("node-needs_you");
    await fillBinding(0, ["max", "1", "anthropic", "subscription", "sonnet", "claude-subscription/v1"]);
    await fillBinding(1, ["review-key", "2", "openai", "api_key", "gpt-5.6-sol", "codex/v1"]);
    expect(screen.queryByText("Complete every field.")).toBeNull();
    expect(cockpitApi.publishAuthProfile).not.toHaveBeenCalled();
    await fireEvent.click(screen.getByRole("button", { name: "Start" }));
    expect(screen.getByRole("status").textContent).toContain("Starting the exact run");
    expect(screen.getByLabelText("Saved workflow")).toHaveProperty("disabled", true);
    expect(screen.getByRole("article", { name: "Binding builder" }).classList).toContain("node-working");
    rejectConfiguration(new Error("config offline"));
    expect(await screen.findByText("config offline")).toBeTruthy();
    expect(cockpitApi.publishAuthProfile).toHaveBeenLastCalledWith({ profile_id: "max", revision_number: 1, provider_id: "anthropic", auth_mode: "subscription" });
    expect(cockpitApi.publishAgentConfiguration).toHaveBeenLastCalledWith({ model: "sonnet", auth_profile_revision_hash: authHash, executor_revision: "claude-subscription/v1" });
    expect((screen.getAllByLabelText("Profile ID")[0] as HTMLInputElement).value).toBe("max");
    await fireEvent.click(screen.getByRole("button", { name: "Start" }));
    expect(await screen.findByText("The start response changed the exact role bindings.")).toBeTruthy();
    await fireEvent.click(await screen.findByRole("button", { name: "Retry" }));
    expect(screen.getByRole("status").textContent).toContain("Retrying exact request");
    expect(screen.getByRole("article", { name: "Binding builder" }).classList).toContain("node-queued");
    continueRetry();
    await waitFor(() => expect(screen.getByRole("button", { name: "Retry" })).toHaveProperty("disabled", false));
    await fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(cockpitApi.start).toHaveBeenCalledTimes(3));
    expect(jsonBody(vi.mocked(cockpitApi.start).mock.calls[2]?.[0])).toEqual({
      workflow_format_version: 2,
      run_id: "run-v2",
      workflow_revision_hash: publishedRevision!.revision_hash,
      agent_bindings: [
        { role: "reviewer", agent_configuration_revision_hash: "c".repeat(64) },
        { role: "builder", agent_configuration_revision_hash: "d".repeat(64) }
      ]
    });
    const card = await screen.findByRole("article", { name: "build — Working" });
    expect(card.textContent).toContain("builder");
    expect(card.textContent).toContain("Attempt 1 prepared");
    eventFeed.handlers?.event(JSON.stringify(v2TerminalEvent(publishedRevision!.revision_hash)));
    expect((await screen.findByRole("article", { name: "build — Completed" })).textContent).toContain("Attempt 1 completed");
    expect(screen.getByRole("article", { name: "review — Working" })).toBeTruthy();
    eventFeed.handlers?.event(JSON.stringify({ ...v2TerminalEvent(publishedRevision!.revision_hash), event: "NODE_PROGRESS" }));
    expect(await screen.findByText("Event invalid")).toBeTruthy();
  });

  it("cancels publication with Escape, restores focus, and sends no bytes", async () => {
    window.history.replaceState(null, "", "/atelier/new");
    const cockpitApi = api();
    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });
    await fireEvent.click(await screen.findByLabelText("Publish YAML"));
    await fireEvent.input(screen.getByLabelText("Exact workflow YAML"), {
      target: { value: "format_version: 1\n" }
    });
    const review = screen.getByRole("button", { name: "Review publication" });
    await fireEvent.click(review);
    expect(document.activeElement).toBe(screen.getByRole("dialog"));

    await fireEvent.keyDown(window, { key: "Escape" });

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(review);
    expect(cockpitApi.publish).not.toHaveBeenCalled();
  });

  it("discards a stale saved-workflow response after switching to publication", async () => {
    window.history.replaceState(null, "", "/atelier/new");
    let resolveRevision = (revision: WorkflowRevisionDetail): void => void revision;
    const deferred = new Promise<WorkflowRevisionDetail>((resolve) => { resolveRevision = resolve; });
    render(App, {
      props: { cockpitApi: api({ getWorkflowRevision: vi.fn(() => deferred) }), mutationJournal: new MutationJournal(sessionStorage) }
    });
    await fireEvent.click(await screen.findByLabelText(revisionHash));
    expect(screen.getByText("Loading workflow…")).toBeTruthy();
    await fireEvent.click(screen.getByLabelText("Publish YAML"));
    resolveRevision(revision());

    await waitFor(() => expect(screen.queryByRole("button", { name: "Start" })).toBeNull());
  });

  it("keeps an ambiguous start byte-identical and exposes Retry or Discard after reload", async () => {
    window.history.replaceState(null, "", "/atelier/new");
    const cockpitApi = api({ start: vi.fn().mockRejectedValue(new Error("connection closed")) });
    const journal = new MutationJournal(sessionStorage);
    const first = render(App, {
      props: { cockpitApi, mutationJournal: journal, createRunId: () => "run-draft" }
    });
    await fireEvent.click(await screen.findByLabelText(revisionHash));
    await fireEvent.click(screen.getByRole("button", { name: "Start" }));
    expect(await screen.findByRole("alert")).toHaveProperty("textContent", expect.stringContaining("connection closed"));
    const firstBytes = vi.mocked(cockpitApi.start).mock.calls[0]?.[0].body_base64;

    first.unmount();
    render(App, { props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) } });
    await fireEvent.click(await screen.findByRole("button", { name: "Retry" }));

    await waitFor(() => expect(cockpitApi.start).toHaveBeenCalledTimes(2));
    expect(vi.mocked(cockpitApi.start).mock.calls[1]?.[0].body_base64).toBe(firstBytes);
    expect(screen.getByRole("button", { name: "Discard" }).isConnected).toBe(true);
  });

  it("keeps an ambiguous publication as an exact uncertain retry", async () => {
    window.history.replaceState(null, "", "/atelier/new");
    const cockpitApi = api({ publish: vi.fn().mockRejectedValue(new Error("connection closed")) });
    const journal = new MutationJournal(sessionStorage);
    render(App, { props: { cockpitApi, mutationJournal: journal } });
    await fireEvent.click(await screen.findByLabelText("Publish YAML"));
    await fireEvent.input(screen.getByLabelText("Exact workflow YAML"), {
      target: { value: "format_version: 1\n" }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Review publication" }));
    await fireEvent.click(screen.getByRole("button", { name: "Publish" }));

    await screen.findByRole("alert");
    expect((await journal.entries())[0]).toMatchObject({ kind: "publish", delivery: "uncertain" });
    expect(screen.getByRole("button", { name: "Retry" }).isConnected).toBe(true);
  });

  it("removes an exact start after a typed HTTP rejection proves it was not applied", async () => {
    window.history.replaceState(null, "", "/atelier/new");
    const failure = new CockpitRequestError(
      "Run not found",
      {
        type: "urn:atelier2:problem:v1:run-not-found",
        title: "Run not found",
        status: 404,
        detail: "Run not found"
      },
      true
    );
    const cockpitApi = api({ start: vi.fn().mockRejectedValue(failure) });
    const journal = new MutationJournal(sessionStorage);
    render(App, {
      props: { cockpitApi, mutationJournal: journal, createRunId: () => "run-draft" }
    });
    await fireEvent.click(await screen.findByLabelText(revisionHash));
    await fireEvent.click(screen.getByRole("button", { name: "Start" }));

    expect(await screen.findByRole("alert")).toHaveProperty("textContent", expect.stringContaining("Run not found"));
    expect(await journal.entries()).toEqual([]);
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("keeps an exact start uncertain when a typed server failure may follow a durable write", async () => {
    window.history.replaceState(null, "", "/atelier/new");
    const failure = new CockpitRequestError(
      "Temporarily unavailable",
      {
        type: "urn:atelier2:problem:v1:temporarily-unavailable",
        title: "Temporarily unavailable",
        status: 503,
        detail: "Temporarily unavailable"
      },
      false
    );
    const cockpitApi = api({ start: vi.fn().mockRejectedValue(failure) });
    const journal = new MutationJournal(sessionStorage);
    render(App, {
      props: { cockpitApi, mutationJournal: journal, createRunId: () => "run-draft" }
    });
    await fireEvent.click(await screen.findByLabelText(revisionHash));
    await fireEvent.click(screen.getByRole("button", { name: "Start" }));

    await screen.findByRole("alert");
    expect((await journal.entries())[0]).toMatchObject({ kind: "start", delivery: "uncertain" });
    expect(screen.getByRole("button", { name: "Retry" }).isConnected).toBe(true);
  });

  it("start_opens_stable_run_url_and_reload_restores_it", async () => {
    window.history.replaceState(null, "", "/atelier/new");
    const cockpitApi = api();
    const first = render(App, {
      props: {
        cockpitApi,
        mutationJournal: new MutationJournal(sessionStorage),
        createRunId: () => "run-draft"
      }
    });
    await fireEvent.click(await screen.findByLabelText(revisionHash));
    await fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(window.location.pathname).toBe(`/atelier/runs/${publicReference}`));

    first.unmount();
    render(App, { props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) } });

    expect((await screen.findByRole("heading", { name: "Run run-draft" })).isConnected).toBe(true);
    expect(cockpitApi.getRun).toHaveBeenLastCalledWith(publicReference);
  });
});

describe("same-origin API transport", () => {
  it("uses bounded list queries and preserves exact journal bytes", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ items: [run()], next_after: null }))
      .mockResolvedValueOnce(jsonResponse(revision(), 201));
    const client = createCockpitApi(fetcher);
    const publication = publicationMutation("Grüße 東京\n");

    await client.listRuns();
    await client.publish(publication);

    expect(fetcher.mock.calls[0]?.[0]).toBe("/atelier/api/v1/runs?limit=50");
    expect(fetcher.mock.calls[1]?.[0]).toBe("/atelier/api/v1/workflow-revisions");
    expect(await requestText(fetcher.mock.calls[1]?.[1])).toBe("Grüße 東京\n");
  });

  it("fails closed on an undocumented response instead of trusting its body", async () => {
    const client = createCockpitApi(
      vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ items: [], next_after: null }, 206))
    );

    await expect(client.listRuns()).rejects.toThrow("undocumented HTTP 206");
  });

  it("treats typed server errors as ambiguous for durable mutations", async () => {
    const client = createCockpitApi(
      vi.fn<typeof fetch>().mockResolvedValue(
        jsonResponse(
          {
            type: "urn:atelier2:problem:v1:temporarily-unavailable",
            title: "Temporarily unavailable",
            status: 503,
            detail: "Retry later"
          },
          503
        )
      )
    );

    await expect(client.start(startRequest())).rejects.toMatchObject({ definitive_failure: false });
  });

  it("does not trust a problem body whose status disagrees with HTTP", async () => {
    const client = createCockpitApi(
      vi.fn<typeof fetch>().mockResolvedValue(
        jsonResponse(
          {
            type: "urn:atelier2:problem:v1:run-not-found",
            title: "Run not found",
            status: 404,
            detail: "Run not found"
          },
          503
        )
      )
    );

    await expect(client.start(startRequest())).rejects.toMatchObject({
      definitive_failure: false,
      problem: null
    });
  });
});

function api(overrides: Partial<CockpitApi> = {}): CockpitApi {
  return {
    listRuns: vi.fn(async () => ({ items: [], next_after: null })),
    listWorkflowRevisions: vi.fn(async () => ({
      items: [{ revision_hash: revisionHash }],
      next_after_revision_hash: null
    })),
    publish: vi.fn(async () => ({ status: 201, value: revision() })),
    publishAuthProfile: vi.fn(),
    publishAgentConfiguration: vi.fn(),
    start: vi.fn(async () => ({ status: 201, value: run() })),
    answer: vi.fn(),
    reconcile: vi.fn(),
    getRun: vi.fn(async () => run()),
    getWorkflowRevision: vi.fn(async () => revision()),
    openRunEvents: vi.fn(() => ({ close: vi.fn() })),
    ...overrides
  };
}

function run(): RunV1 {
  return {
    run_id: "run-draft",
    public_run_reference: publicReference,
    workflow_revision_hash: revisionHash,
    state_version: 0,
    state: "STARTED",
    current_node: {
      type: "agent",
      node_id: "agent",
      job: "Build the feature",
      output: "result",
      next_node_id: "done"
    },
    waiting: { type: "NONE" },
    terminal_hash: null,
    latest_event_cursor: null
  };
}

function revision(): WorkflowRevisionDetail {
  return { revision_hash: revisionHash, document_base64: "", graph: graph() };
}

function v2Revision(hash: string, documentBase64 = ""): WorkflowRevisionDetail {
  return {
    revision_hash: hash, document_base64: documentBase64,
    graph: {
      format_version: 2, start_node_id: "build",
      nodes: [
        { type: "agent", node_id: "review", role: "reviewer", job: "Review", next_node_id: "fix" },
        { type: "agent", node_id: "build", role: "builder", job: "Build", next_node_id: "review" },
        { type: "agent", node_id: "fix", role: "builder", job: "Fix", next_node_id: "done" },
        { type: "subworkflow", node_id: "done", operation: "add", operands: [1, 1], next_node_id: null }
      ]
    }
  };
}

function v2Run(start: unknown, agentBindings: RunV2["agent_bindings"]): RunV2 {
  const workflowRevisionHash = (start as { workflow_revision_hash: string }).workflow_revision_hash;
  return {
    workflow_format_version: 2,
    run_id: "run-v2",
    public_run_reference: v2PublicReference,
    workflow_revision_hash: workflowRevisionHash,
    agent_binding_set_hash: revisionHash,
    agent_bindings: agentBindings,
    state_version: 0,
    state: "STARTED",
    current_node: v2Revision(workflowRevisionHash).graph.nodes.find((node) => node.node_id === "build")! as RunV2["current_node"],
    agent_attempts: [{ attempt_id: "1".repeat(64), node_execution_id: "2".repeat(64), request_hash: "3".repeat(64),
      attempt_ordinal: 1, state: "PREPARED", failure_code: null, cancellation: null }],
    waiting: { type: "NONE" }, terminal_hash: null, latest_event_cursor: null
  };
}

function v2Bindings(authHash: string): RunV2["agent_bindings"] {
  return [
    { role: "builder", profile_id: "review-key", revision_number: 2, provider_id: "openai", auth_mode: "api_key", model: "gpt-5.6-sol", executor_revision: "codex/v1", auth_profile_revision_hash: authHash, agent_configuration_revision_hash: "d".repeat(64) },
    { role: "reviewer", profile_id: "max", revision_number: 1, provider_id: "anthropic", auth_mode: "subscription", model: "sonnet", executor_revision: "claude-subscription/v1", auth_profile_revision_hash: authHash, agent_configuration_revision_hash: "c".repeat(64) }
  ];
}

function v2TerminalEvent(workflowRevisionHash: string) {
  return {
    workflow_format_version: 2, cursor: "event1.cnVuLXYy.1", sequence: 1,
    public_run_reference: v2PublicReference, workflow_revision_hash: workflowRevisionHash,
    node_id: "build", node_execution_id: "2".repeat(64), event_hash: "4".repeat(64),
    event: "AGENT_COMPLETED", output_base64: "", output_hash: revisionHash,
    attempt_id: "1".repeat(64), attempt_ordinal: 1
  };
}

async function fillBinding(index: number, values: readonly string[]): Promise<void> {
  const labels = ["Profile ID", "Revision", "Provider", "Auth mode", "Model", "Executor"];
  for (const [field, label] of labels.entries()) {
    const control = screen.getAllByLabelText(label)[index]!;
    await (label === "Auth mode" ? fireEvent.change : fireEvent.input)(control, { target: { value: values[field] } });
  }
}

function graph() {
  return {
    format_version: 1 as const,
    start_node_id: "agent",
    nodes: [
      {
        type: "agent" as const,
        node_id: "agent",
        job: "Build the feature",
        output: "result",
        next_node_id: "done"
      },
      {
        type: "subworkflow" as const,
        node_id: "done",
        operation: "add" as const,
        operands: [2, 3] as [number, number],
        next_node_id: null
      }
    ]
  };
}

function publicationMutation(document: string) {
  return {
    mutation_id: `publish:${revisionHash}`,
    kind: "publish" as const,
    target: "/atelier/api/v1/workflow-revisions",
    content_type: "application/yaml" as const,
    body_base64: bytesBase64(new TextEncoder().encode(document)),
    revision_hash: revisionHash
  };
}

function startRequest() {
  const body = bytesBase64(
    new TextEncoder().encode(JSON.stringify({ run_id: "run-draft", workflow_revision_hash: revisionHash }))
  );
  return {
    mutation_id: "start:run-draft",
    kind: "start" as const,
    target: "/atelier/api/v1/runs",
    content_type: "application/json" as const,
    body_base64: body
  };
}

function textBody(mutation: { body_base64: string } | undefined): string {
  if (mutation === undefined) throw new Error("missing mutation");
  return new TextDecoder().decode(base64Bytes(mutation.body_base64));
}

function jsonBody(mutation: { body_base64: string } | undefined): unknown {
  return JSON.parse(textBody(mutation));
}

function base64Bytes(value: string): Uint8Array {
  return Uint8Array.from(atob(value), (character) => character.charCodeAt(0));
}

function bytesBase64(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes));
}

function withinRole(container: HTMLElement, role: string, name: string): HTMLElement {
  const element = Array.from(container.querySelectorAll<HTMLElement>(`[role=${role}], ${role}`)).find(
    (candidate) => candidate.textContent?.trim() === name
  );
  if (element === undefined) throw new Error(`missing ${role} ${name}`);
  return element;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" }
  });
}

async function requestText(init: RequestInit | undefined): Promise<string> {
  if (!(init?.body instanceof ArrayBuffer)) throw new Error("request body is not exact bytes");
  return new TextDecoder().decode(init.body);
}
