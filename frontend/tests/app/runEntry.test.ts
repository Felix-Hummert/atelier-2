import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import NodeRail from "../../src/components/NodeRail.svelte";
import {
  CockpitRequestError,
  createCockpitApi,
  executableGraph,
  type CockpitApi,
  type RunEventHandlers,
  type RunPage,
  type RunEvent,
  type RunV1,
  type RunV2,
  type WorkflowRevisionDetail
} from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { cockpitApiStub, FakeRunEventFeed } from "../support/cockpitApi";
import { base64Bytes, bytesBase64 } from "../support/exactBytes";

const revisionHash = "a".repeat(64);
const publicReference = "run1.cnVuLWRyYWZ0";
const v2PublicReference = "run1.cnVuLXYy";

beforeEach(() => {
  sessionStorage.clear();
  window.history.replaceState(null, "", "/atelier/project");
});

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
});

describe("mobile run entry", () => {
  it("lists a bounded durable run page and offers no manual refresh once confirmed", async () => {
    const listRuns = vi.fn().mockResolvedValue({ items: [run()], next_after: null });
    const cockpitApi = api({
      listRuns,
      listProjects: vi.fn(async () => ({ items: [{ public_project_reference: "project1.dGVzdA" }] }))
    });
    render(App, { props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) } });

    expect((await screen.findByRole("link", { name: /run-draft/i })).getAttribute("href")).toBe(
      `/atelier/runs/${publicReference}`
    );
    expect(screen.queryByRole("button", { name: /project runs/ })).toBeNull();
  });

  it("says the durable run list is still loading instead of showing an empty one", async () => {
    const listRuns = vi.fn(() => new Promise<RunPage>(() => undefined));
    render(App, {
      props: { cockpitApi: api({ listRuns }), mutationJournal: new MutationJournal(sessionStorage) }
    });

    expect(within(await screen.findByRole("status")).getByText("Looking…").isConnected).toBe(true);
    expect(screen.queryByRole("region", { name: "Running" })).toBeNull();
    expect(screen.queryByRole("listitem")).toBeNull();
  });

  it("shows a project with no runs as empty of groups, still naming where a run comes from", async () => {
    const listRuns = vi.fn(async () => ({ items: [], next_after: null }));
    render(App, {
      props: { cockpitApi: api({ listRuns }), mutationJournal: new MutationJournal(sessionStorage) }
    });

    expect((await screen.findByText("No runs here yet.")).isConnected).toBe(true);
    expect(screen.getByRole("link", { name: "Start a run" }).getAttribute("href")).toBe("/atelier/new");
    expect(screen.queryByRole("listitem")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
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

    await fireEvent.click(await screen.findByRole("radio", { name: new RegExp(revisionHash) }));
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
          workflow_revision_hash: mutation.mutation_id.slice("publish:".length),
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

  it("proves(a-revision-no-run-can-start-says-so-where-it-was-published): names a published V3 revision and what stops it, instead of offering Start", async () => {
    window.history.replaceState(null, "", "/atelier/new");
    const cockpitApi = api({
      publish: vi.fn(async (mutation) => ({
        status: 201 as const,
        value: {
          workflow_revision_hash: mutation.mutation_id.slice("publish:".length),
          document_base64: mutation.body_base64,
          graph: {
            workflow_format_version: 3 as const,
            executable: false as const,
            not_executable_reason:
              "agent-output-shape-unavailable: 0 outputs on node 'implement', and an agent node completes with the one value its own schema judges" as const,
            agent_roles: [],
            orders: [],
            node_count: 1,
            node_previews: [
              {
                id: "only",
                kind: "agent" as const,
                role: "builder",
                instruction_start: "Sweep the suite.",
                depends_on: []
              }
            ],
            loops: [],
            name: "Nightly regression sweep",
            description: "Runs the sweep and files what it finds."
          }
        }
      }))
    });
    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });

    await fireEvent.click(await screen.findByLabelText("Publish YAML"));
    await fireEvent.input(screen.getByLabelText("Exact workflow YAML"), {
      target: { value: "format_version: 3\nname: Nightly regression sweep\n" }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Review publication" }));
    const dialog = screen.getByRole("dialog", { name: "Publish this exact workflow?" });
    await fireEvent.click(withinRole(dialog, "button", "Publish"));

    await waitFor(() => expect(cockpitApi.publish).toHaveBeenCalledTimes(1));
    expect((await screen.findByText("Nightly regression sweep")).isConnected).toBe(true);
    // Extension, named: this pinned "format 3 is not executable yet", which is the
    // version blame the server's own rule avoids. The reason it now serves names
    // the authored form nothing binds, which is what the sentence asks for.
    expect(screen.getByText(/Add one outputs: entry/i).isConnected).toBe(true);
    expect(screen.queryByText(/agent-output-shape-unavailable/i)).toBeNull();
    expect(screen.queryByRole("button", { name: "Start" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
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
          requested_capability: input.requested_capability ?? ("headless" as const),
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
      workflow_revision_hash: publishedRevision!.workflow_revision_hash,
      agent_bindings: [
        { role: "reviewer", agent_configuration_revision_hash: "c".repeat(64) },
        { role: "builder", agent_configuration_revision_hash: "d".repeat(64) }
      ]
    });
    const card = await screen.findByRole("article", { name: "build — Working" });
    expect(card.textContent).toContain("builder");
    expect(card.textContent).toContain("Attempt 1 prepared");
    eventFeed.handlers?.event(JSON.stringify(v2TerminalEvent(publishedRevision!.workflow_revision_hash)));
    expect((await screen.findByRole("article", { name: "build — Done" })).textContent).toContain("Attempt 1 done");
    expect(screen.getByRole("article", { name: "review — Working" })).toBeTruthy();
    eventFeed.handlers?.event(JSON.stringify({ ...v2TerminalEvent(publishedRevision!.workflow_revision_hash), event: "NODE_PROGRESS" }));
    expect(await screen.findByText("Event invalid")).toBeTruthy();
  });

  it("shows the prepared replacement attempt as live while its predecessor remains history", () => {
    const initial = v2Run(
      { workflow_revision_hash: revisionHash },
      v2Bindings("b".repeat(64))
    );
    const firstAttempt = initial.agent_attempts[0]!;
    const replacementAttemptId = "5".repeat(64);
    const interrupted = v2InterruptedEvent(revisionHash, replacementAttemptId);
    const replacementRun: RunV2 = {
      ...initial,
      latest_event_cursor: interrupted.cursor,
      node_rail: v2Rail("working", { ordinal: 2, state: "PREPARED" }),
      agent_attempts: [
        {
          ...firstAttempt,
          state: "INTERRUPTED",
          cancellation: {
            command_id: "cancel",
            replacement: "ONE",
            redrive_state: "CLEANUP_ATTESTED",
            disposition: "REAPED_AFTER_TERM"
          }
        },
        {
          ...firstAttempt,
          attempt_id: replacementAttemptId,
          attempt_ordinal: 2,
          state: "PREPARED"
        }
      ]
    };

    render(NodeRail, {
      props: { run: replacementRun, graph: executableGraph(v2Revision(revisionHash).graph), events: [interrupted] }
    });

    const card = screen.getByRole("article", { name: "build — Working" });
    expect(card.textContent).toContain("Attempt 2 prepared");
    expect(card.textContent).toContain("AGENT INTERRUPTED");
  });

  it("shows byte-verified V2 output and preserves synchronous event arrival order", async () => {
    window.history.replaceState(null, "", `/atelier/runs/${v2PublicReference}`);
    const feed = new FakeRunEventFeed();
    const digestImplementation = globalThis.crypto.subtle.digest.bind(globalThis.crypto.subtle);
    let releaseFirstDigest = (): void => {};
    const firstDigestGate = new Promise<void>((resolve) => { releaseFirstDigest = resolve; });
    let digestCalls = 0;
    const digestSpy = vi.spyOn(globalThis.crypto.subtle, "digest").mockImplementation(async (...arguments_) => {
      if (++digestCalls === 1) await firstDigestGate;
      return digestImplementation(...arguments_);
    });
    render(App, {
      props: {
        cockpitApi: api({
          getRun: vi.fn(async () => v2Run({ workflow_revision_hash: revisionHash }, v2Bindings("b".repeat(64)))),
          getWorkflowRevision: vi.fn(async () => v2Revision(revisionHash)),
          openRunEvents: feed.open
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    expect(await screen.findByRole("article", { name: "build — Working" })).toBeTruthy();
    feed.handlers?.event(JSON.stringify(v2CompletedEvent({
      output_base64: "R3LDvMOfZSDmnbHkuqw=",
      output_hash: "d9f1fa3818c49d96dce2661015bdad90989df9e67244a7e5f1519ab466286332"
    })));
    feed.handlers?.event(JSON.stringify(v2CompletedEvent({
      cursor: "event1.cnVuLXYy.2",
      sequence: 2,
      node_id: "review",
      output_base64: "/wA=",
      output_hash: "ea5dbf9596d187e9500f23e9a680109475341cf4e81f7e043f7d97152c10772f"
    })));

    await waitFor(() => expect(digestCalls).toBe(1));
    releaseFirstDigest();
    const output = await screen.findByRole("region", { name: "Verified output" });
    expect(output.textContent).toContain("UTF-8");
    expect(output.textContent).toContain("14 bytes");
    expect(output.textContent).toContain("Verified");
    expect(output.textContent).toContain("Grüße 東京");
    expect(await screen.findByText("#2 · review")).toBeTruthy();
    digestSpy.mockRestore();
  });

  it("closes the stream and keeps contradictory V2 output out of the cockpit", async () => {
    window.history.replaceState(null, "", `/atelier/runs/${v2PublicReference}`);
    const feed = new FakeRunEventFeed();
    render(App, {
      props: {
        cockpitApi: api({
          getRun: vi.fn(async () => v2Run({ workflow_revision_hash: revisionHash }, v2Bindings("b".repeat(64)))),
          getWorkflowRevision: vi.fn(async () => v2Revision(revisionHash)),
          openRunEvents: feed.open
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    expect(await screen.findByRole("article", { name: "build — Working" })).toBeTruthy();
    feed.handlers?.event(JSON.stringify(v2CompletedEvent({
      output_base64: "R3LDvMOfZSDmnbHkuqw=",
      output_hash: revisionHash
    })));

    expect((await screen.findByRole("alert")).textContent).toContain("Output mismatch");
    expect(screen.getByRole("status").textContent).toContain("Stopped");
    expect(screen.queryByText("Grüße 東京")).toBeNull();
    expect(screen.getByRole("article", { name: "build — Working" })).toBeTruthy();
    expect(feed.close).toHaveBeenCalledTimes(1);
  });

  it("reports the durable corruption the stream named instead of a finished run", async () => {
    window.history.replaceState(null, "", `/atelier/runs/${v2PublicReference}`);
    const feed = new FakeRunEventFeed();
    render(App, {
      props: {
        cockpitApi: api({
          getRun: vi.fn(async () => v2Run({ workflow_revision_hash: revisionHash }, v2Bindings("b".repeat(64)))),
          getWorkflowRevision: vi.fn(async () => v2Revision(revisionHash)),
          openRunEvents: feed.open
        }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    expect(await screen.findByRole("article", { name: "build — Working" })).toBeTruthy();
    feed.handlers?.opened();
    feed.handlers?.event(JSON.stringify({
      event: "STREAM_FAILED",
      problem: {
        type: "urn:atelier2:problem:v1:durable-state-corrupt",
        title: "Durable state is corrupt",
        status: 500,
        detail: "Stop mutation and inspect the durable store."
      }
    }));

    const notice = await screen.findByRole("alert");
    expect(notice.textContent).toContain("Durable state is corrupt");
    expect(notice.textContent).toContain("Refresh the page");
    expect(notice.textContent).not.toContain("Stop mutation and inspect the durable store.");
    const badge = screen.getByRole("status").textContent;
    expect(badge).toContain("Stopped");
    expect(badge).not.toContain("Live");
    expect(badge).not.toContain("Complete");
    expect(feed.close).toHaveBeenCalledTimes(1);
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

  it("keeps an unconfirmed saved-workflow radio unchecked until exact Retry supplies its draft", async () => {
    window.history.replaceState(null, "", "/atelier/new");
    const getWorkflowRevision = vi
      .fn()
      .mockRejectedValueOnce(new Error("private detail failure"))
      .mockResolvedValueOnce(revision());
    render(App, {
      props: {
        cockpitApi: api({ getWorkflowRevision }),
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });
    const option = await screen.findByRole("radio", { name: new RegExp(revisionHash) });

    await fireEvent.click(option);
    expect(await screen.findByText("Workflow detail unavailable")).toBeTruthy();
    expect((option as HTMLInputElement).checked).toBe(false);
    expect(screen.queryByRole("heading", { name: "Run ID" })).toBeNull();
    expect(screen.queryByText(/private detail failure/i)).toBeNull();

    await fireEvent.click(screen.getByRole("button", { name: "Retry workflow detail" }));
    await waitFor(() => expect((option as HTMLInputElement).checked).toBe(true));
    expect(screen.getByRole("heading", { name: "Run ID" }).isConnected).toBe(true);
    expect(getWorkflowRevision.mock.calls.map(([hash]) => hash)).toEqual([
      revisionHash,
      revisionHash
    ]);
  });

  it("keeps a published draft when an earlier saved-detail response arrives late", async () => {
    window.history.replaceState(null, "", "/atelier/new");
    let publishedHash = "";
    let resolveRevision = (revision: WorkflowRevisionDetail): void => void revision;
    const deferred = new Promise<WorkflowRevisionDetail>((resolve) => { resolveRevision = resolve; });
    const cockpitApi = api({
      getWorkflowRevision: vi.fn(() => deferred),
      publish: vi.fn(async (mutation) => {
        publishedHash = mutation.mutation_id.slice("publish:".length);
        return {
          status: 201,
          value: {
            ...revision(),
            workflow_revision_hash: publishedHash,
            document_base64: mutation.body_base64
          }
        };
      })
    });
    render(App, {
      props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) }
    });
    await fireEvent.click(await screen.findByRole("radio", { name: new RegExp(revisionHash) }));
    expect((await screen.findByText("Looking…")).isConnected).toBe(true);
    expect(screen.queryByRole("button", { name: /workflow detail/ })).toBeNull();
    await fireEvent.click(screen.getByLabelText("Publish YAML"));
    await fireEvent.input(screen.getByLabelText("Exact workflow YAML"), {
      target: { value: "format_version: 1\nstart_node_id: agent\n" }
    });
    await fireEvent.click(screen.getByRole("button", { name: "Review publication" }));
    await fireEvent.click(
      within(screen.getByRole("dialog", { name: "Publish this exact workflow?" }))
        .getByRole("button", { name: "Publish" })
    );
    await screen.findByRole("button", { name: "Start" });
    resolveRevision(revision());

    await fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(cockpitApi.start).toHaveBeenCalledTimes(1));
    expect(jsonBody(vi.mocked(cockpitApi.start).mock.calls[0]?.[0])).toMatchObject({
      workflow_revision_hash: publishedHash
    });
  });

  it("keeps an ambiguous start byte-identical and exposes Retry or Discard after reload", async () => {
    window.history.replaceState(null, "", "/atelier/new");
    const cockpitApi = api({ start: vi.fn().mockRejectedValue(new Error("connection closed")) });
    const journal = new MutationJournal(sessionStorage);
    const first = render(App, {
      props: { cockpitApi, mutationJournal: journal, createRunId: () => "run-draft" }
    });
    await fireEvent.click(await screen.findByRole("radio", { name: new RegExp(revisionHash) }));
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
    await fireEvent.click(await screen.findByRole("radio", { name: new RegExp(revisionHash) }));
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
    await fireEvent.click(await screen.findByRole("radio", { name: new RegExp(revisionHash) }));
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
    await fireEvent.click(await screen.findByRole("radio", { name: new RegExp(revisionHash) }));
    await fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(window.location.pathname).toBe(`/atelier/runs/${publicReference}`));

    first.unmount();
    render(App, { props: { cockpitApi, mutationJournal: new MutationJournal(sessionStorage) } });

    expect(
      (await screen.findByRole("heading", { name: "Unnamed workflow" })).isConnected
    ).toBe(true);
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
  return cockpitApiStub({
    listWorkflowRevisions: vi.fn(async () => ({
      items: [
        {
          workflow_revision_hash: revisionHash,
          workflow_format_version: 2 as const,
          executable: true,
          not_executable_reason: null,
          name: null,
          description: null
        }
      ],
      next_after_revision_hash: null
    })),
    publish: vi.fn(async () => ({ status: 201, value: revision() })),
    start: vi.fn(async () => ({ status: 201, value: run() })),
    getRun: vi.fn(async () => run()),
    getWorkflowRevision: vi.fn(async () => revision()),
    ...overrides
  });
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
  return { workflow_revision_hash: revisionHash, document_base64: "", graph: graph() };
}

function v2Revision(hash: string, documentBase64 = ""): WorkflowRevisionDetail {
  return {
    workflow_revision_hash: hash, document_base64: documentBase64,
    graph: {
      workflow_format_version: 2, start_node_id: "build",
      nodes: [
        { type: "agent", node_id: "review", role: "reviewer", job: "Review", next_node_id: "fix" },
        { type: "agent", node_id: "build", role: "builder", job: "Build", next_node_id: "review" },
        { type: "agent", node_id: "fix", role: "builder", job: "Fix", next_node_id: "done" },
        { type: "subworkflow", node_id: "done", operation: "add", operands: [1, 1], next_node_id: null }
      ]
    }
  };
}

function v2Rail(
  build: RunV2["node_rail"][number]["state"],
  attempt: RunV2["node_rail"][number]["attempt"]
): RunV2["node_rail"] {
  const successor = build === "working" ? "queued" : "working";
  return [
    { node_id: "build", state: build, attempt },
    { node_id: "review", state: successor, attempt: null },
    { node_id: "fix", state: "queued", attempt: null },
    { node_id: "done", state: "queued", attempt: null }
  ];
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
    current_node: executableGraph(v2Revision(workflowRevisionHash).graph).nodes.find((node) => node.node_id === "build")! as RunV2["current_node"],
    node_rail: v2Rail("working", { ordinal: 1, state: "PREPARED" }),
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
    event: "AGENT_COMPLETED", output_base64: "", output_hash: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    attempt_id: "1".repeat(64), attempt_ordinal: 1,
    node_rail: v2Rail("succeeded", { ordinal: 1, state: null })
  };
}

function v2CompletedEvent(changes: Record<string, unknown> = {}) {
  return {
    ...v2TerminalEvent(revisionHash),
    ...changes
  };
}

function v2InterruptedEvent(
  workflowRevisionHash: string,
  replacementAttemptId: string
): RunEvent {
  return {
    workflow_format_version: 2,
    cursor: "event1.cnVuLXYy.1",
    sequence: 1,
    public_run_reference: v2PublicReference,
    workflow_revision_hash: workflowRevisionHash,
    node_id: "build",
    node_execution_id: "2".repeat(64),
    event_hash: "4".repeat(64),
    event: "AGENT_INTERRUPTED",
    attempt_id: "1".repeat(64),
    attempt_ordinal: 1,
    node_rail: v2Rail("working", { ordinal: 2, state: "PREPARED" }),
    command_id: "cancel",
    replacement: "ONE",
    disposition: "REAPED_AFTER_TERM",
    replacement_attempt_id: replacementAttemptId
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
    workflow_format_version: 1 as const,
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
