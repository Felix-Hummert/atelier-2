import { beforeEach, describe, expect, it, vi } from "vitest";

import type { CockpitApi, RunV3, WorkflowRevisionDetail } from "../../src/api/client";
import { conductorChatCopy } from "../../src/lib/conductorChatCopy";
import { cockpitApiStub, FakeRunEventFeed } from "../support/cockpitApi";

/**
 * The conversation lives in module state (it outlives the page), so every
 * test boots a fresh module instance -- the same isolation a reload gives
 * the operator, the pattern `tests/app/workbenchPage.test.ts` set. The client
 * module is imported from the same fresh runtime, so a thrown
 * `CockpitRequestError` is an instance of the class the episode module holds.
 */
async function bootModules() {
  vi.resetModules();
  return {
    episode: await import("../../src/lib/conductorEpisode"),
    conversation: await import("../../src/lib/chatTranscript"),
    client: await import("../../src/api/client")
  };
}

type ClientModule = Awaited<ReturnType<typeof bootModules>>["client"];

const CONFIGURATION_HASH = "d".repeat(64);
const REVISION_HASH = "b".repeat(64);
const PUBLIC_RUN = "run1.cnVu";

function problemError(
  client: ClientModule,
  code: "catalog-name-not-found"
): Error {
  return new client.CockpitRequestError(
    code,
    client.decodeProblem({
      type: `urn:atelier2:problem:v1:${code}`,
      title: "Catalog name not found",
      status: 404,
      detail: "not there"
    })
  );
}

function conductorRevisionDetail(): WorkflowRevisionDetail {
  return {
    workflow_revision_hash: REVISION_HASH,
    document_base64: btoa("format_version: 3"),
    graph: {
      workflow_format_version: 3,
      executable: true,
      not_executable_reason: null,
      node_count: 1,
      agent_roles: ["conductor"],
      orders: [{ name: "brief", schema: { ref: "conductor-brief", revision: "c".repeat(64) } }],
      wait_answer_schemas: [],
      node_previews: [],
      loops: [],
      name: "conductor",
      description: null
    }
  };
}

function connectedApiStub(overrides: Partial<CockpitApi> = {}): CockpitApi {
  return cockpitApiStub({
    getRevisionByName: vi.fn(async () => ({
      display_name: "conductor",
      lineage_id: "a".repeat(64),
      workflow_revision_hash: REVISION_HASH,
      revision_number: 1
    })),
    getWorkflowRevision: vi.fn(async () => conductorRevisionDetail()),
    listProjects: vi.fn(async () => ({ items: [{ public_project_reference: "proj1.d29ya3Nob3A" }] })),
    resolveProjectModels: vi.fn(async () => ({
      project_id: "workshop",
      public_project_reference: "proj1.d29ya3Nob3A",
      workflow_revision_hash: REVISION_HASH,
      resolutions: [{
        role: "conductor",
        agent_configuration_revision_hash: CONFIGURATION_HASH,
        source: "from-project" as const,
        model_id: "sonnet",
        declared_difficulty: 2 as const,
        default_difficulty: 2 as const,
        uncast_reason: null,
        family_differs_from: null
      }]
    })),
    listAgentConfigurationRevisions: vi.fn(async () => ({
      items: [configurationItem(true)],
      next_after_revision_hash: null
    })),
    ...overrides
  });
}

function configurationItem(startable: boolean) {
  return {
    model: "sonnet",
    auth_profile_revision_hash: "f".repeat(64),
    executor_revision: "claude-atelier-doors/v1",
    requested_capability: "headless_with_tools" as const,
    provider_id: "anthropic",
    auth_mode: "subscription" as const,
    agent_configuration_revision_hash: CONFIGURATION_HASH,
    startable,
    not_startable_reason: startable ? null : ("agent-executor-binding-unavailable" as const)
  };
}

function startedRun(): { status: number; value: RunV3 } {
  return { status: 201, value: { public_run_reference: PUBLIC_RUN } as unknown as RunV3 };
}

function completedEvent(output: object): object {
  const output_base64 = btoa(JSON.stringify(output));
  return {
    workflow_format_version: 3,
    cursor: "event1.cnVu.3",
    sequence: 3,
    public_run_reference: PUBLIC_RUN,
    workflow_revision_hash: REVISION_HASH,
    node_id: "conduct",
    node_execution_id: "1".repeat(64),
    event_hash: "2".repeat(64),
    node_rail: [{ node_id: "conduct", state: "succeeded", attempt: null }],
    event: "AGENT_COMPLETED",
    output_base64,
    output_hash: "3".repeat(64),
    attempt_id: "4".repeat(64),
    attempt_ordinal: 1
  };
}

/** The code the stream carries and the chat line must no longer repeat (#664). */
const REFUSED_FAILURE_CODE = "OUTPUT_SCHEMA_REFUSED";

function failedEvent(): object {
  return {
    workflow_format_version: 3,
    cursor: "event1.cnVu.3",
    sequence: 3,
    public_run_reference: PUBLIC_RUN,
    workflow_revision_hash: REVISION_HASH,
    node_id: "conduct",
    node_execution_id: "1".repeat(64),
    event_hash: "2".repeat(64),
    node_rail: [{ node_id: "conduct", state: "failed", attempt: null }],
    event: "AGENT_FAILED",
    failure_code: REFUSED_FAILURE_CODE,
    reason: "instance-not-json",
    attempt_id: "4".repeat(64),
    attempt_ordinal: 1
  };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("whether a conductor is connected", () => {
  it("resolves the catalog name, project model, and startable configuration into one connection", async () => {
    const { episode } = await bootModules();

    const connection = await episode.resolveConductorConnection(connectedApiStub());

    expect(connection).toEqual({
      workflowRevisionHash: REVISION_HASH,
      role: "conductor",
      briefOrderName: "brief",
      agentConfigurationRevisionHash: CONFIGURATION_HASH
    });
  });

  it("is not connected while the catalog carries no conductor", async () => {
    const { episode, client } = await bootModules();
    const api = connectedApiStub({
      getRevisionByName: vi.fn(async () => {
        throw problemError(client, "catalog-name-not-found");
      })
    });

    expect(await episode.resolveConductorConnection(api)).toBeNull();
  });

  it("is not connected while the project leaves its role uncast", async () => {
    const { episode } = await bootModules();
    const api = connectedApiStub({
      resolveProjectModels: vi.fn(async () => ({
        project_id: "workshop",
        public_project_reference: "proj1.d29ya3Nob3A",
        workflow_revision_hash: REVISION_HASH,
        resolutions: [{
          role: "conductor",
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

    expect(await episode.resolveConductorConnection(api)).toBeNull();
  });

  it("is not connected while the bound configuration cannot start here", async () => {
    const { episode } = await bootModules();
    const api = connectedApiStub({
      listAgentConfigurationRevisions: vi.fn(async () => ({
        items: [configurationItem(false)],
        next_after_revision_hash: null
      }))
    });

    expect(await episode.resolveConductorConnection(api)).toBeNull();
  });

  it("stays loud on a read failure instead of dressing it as not connected", async () => {
    const { episode } = await bootModules();
    const api = connectedApiStub({
      getRevisionByName: vi.fn(async () => {
        throw new Error("the catalog read broke");
      })
    });

    await expect(episode.resolveConductorConnection(api)).rejects.toThrow("the catalog read broke");
  });
});

describe("the brief a message travels as", () => {
  it("carries the message, the prior transcript in the brief's own speaker tokens, and a zero drop count", async () => {
    const { episode } = await bootModules();

    const brief = episode.conductorBrief(
      [
        { id: "you-1", speaker: "you", text: "hello" },
        { id: "house-1", speaker: "house", text: "hello back" },
        { id: "house-2", speaker: "house", text: "still reading", pending: true }
      ],
      "Start the canary"
    );

    expect(JSON.parse(brief.value)).toEqual({
      message: "Start the canary",
      prior_transcript: [
        { speaker: "operator", text: "hello" },
        { speaker: "conductor", text: "hello back" }
      ],
      dropped_oldest_messages: 0
    });
  });

  it("drops the oldest messages first to fit the inline-order ceiling, and says how many", async () => {
    const { episode } = await bootModules();
    const long = "x".repeat(9_000);

    const brief = episode.conductorBrief(
      [
        { id: "you-1", speaker: "you", text: `first ${long}` },
        { id: "you-2", speaker: "you", text: `second ${long}` },
        { id: "you-3", speaker: "you", text: "third stays" }
      ],
      "Start the canary"
    );

    const value = JSON.parse(brief.value) as {
      prior_transcript: { text: string }[];
      dropped_oldest_messages: number;
    };
    expect(new TextEncoder().encode(brief.value).length).toBeLessThanOrEqual(
      episode.MAXIMUM_BRIEF_BYTES
    );
    expect(value.dropped_oldest_messages).toBe(1);
    expect(value.prior_transcript.map((line) => line.text.slice(0, 6))).toEqual([
      "second",
      "third "
    ]);
  });
});

describe("one message, one conductor episode", () => {
  it("starts one run and settles the reply line with the report's answer and started runs", async () => {
    const { episode, conversation } = await bootModules();
    const feed = new FakeRunEventFeed();
    const start = vi.fn(async () => startedRun());
    const api = connectedApiStub({ start, openRunEvents: feed.open });
    const connection = await episode.resolveConductorConnection(api);
    expect(connection).not.toBeNull();
    if (connection === null) return;

    episode.sendConductorMessage(api, connection, "Start the canary");
    await vi.waitFor(() => expect(feed.handlers).not.toBeNull());
    feed.handlers?.event(
      JSON.stringify(
        completedEvent({ answer: "Started canary; it is STARTED.", started_run_ids: ["canary-1"] })
      )
    );

    const transcript = conversation.currentChatTranscript();
    expect(transcript.map((line) => [line.speaker, line.text])).toEqual([
      ["you", "Start the canary"],
      ["house", `Started canary; it is STARTED.\n${conductorChatCopy.startedRuns} canary-1`]
    ]);
    expect(transcript[1]?.pending).toBe(false);
    expect(transcript[1]?.runReference).toBe(PUBLIC_RUN);
    expect(feed.close).toHaveBeenCalled();
    const startBody = JSON.parse(
      atob((start.mock.calls[0] as unknown as [{ body_base64: string }])[0].body_base64)
    ) as { orders: { name: string; value: string }[]; agent_bindings: object[] };
    expect(startBody.agent_bindings).toEqual([
      { role: "conductor", agent_configuration_revision_hash: CONFIGURATION_HASH }
    ]);
    expect(startBody.orders[0]?.name).toBe("brief");
    expect(JSON.parse(startBody.orders[0]?.value ?? "")).toMatchObject({
      message: "Start the canary"
    });
  });

  it("settles a refused episode in one human sentence beside its own run", async () => {
    const { episode, conversation } = await bootModules();
    const feed = new FakeRunEventFeed();
    const api = connectedApiStub({ start: vi.fn(async () => startedRun()), openRunEvents: feed.open });
    const connection = await episode.resolveConductorConnection(api);
    if (connection === null) throw new Error("expected a connection");

    episode.sendConductorMessage(api, connection, "Do something");
    await vi.waitFor(() => expect(feed.handlers).not.toBeNull());
    feed.handlers?.event(JSON.stringify(failedEvent()));

    const reply = conversation.currentChatTranscript().at(-1);
    expect(reply?.text).toBe(conductorChatCopy.episodeFailed);
    expect(reply?.text).not.toContain(REFUSED_FAILURE_CODE);
    expect(reply?.runReference).toBe(PUBLIC_RUN);
    expect(reply?.pending).toBe(false);
  });

  it("settles the reply line honestly when the start itself is refused", async () => {
    const { episode, conversation } = await bootModules();
    const api = connectedApiStub({
      start: vi.fn(async () => {
        throw new Error("run-input-refused");
      })
    });
    const connection = await episode.resolveConductorConnection(api);
    if (connection === null) throw new Error("expected a connection");

    episode.sendConductorMessage(api, connection, "Do something");
    await vi.waitFor(() => {
      const reply = conversation.currentChatTranscript().at(-1);
      expect(reply?.pending).toBe(false);
      expect(reply?.text).toBe(`${conductorChatCopy.startRefused} run-input-refused`);
    });
  });
});
