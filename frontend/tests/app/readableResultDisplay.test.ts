import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { NodeDetail, RunV3 } from "../../src/api/client";
import { MAXIMUM_REFUSED_OUTPUT_BASE64_CHARACTERS, nodeDetailSchema } from "../../src/api/client";
import NodeDetailPanel from "../../src/components/NodeDetailPanel.svelte";
import V3AnswerCard from "../../src/components/V3AnswerCard.svelte";
import V3RunView from "../../src/components/V3RunView.svelte";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { runPageCopy } from "../../src/lib/runPageCopy";
import { runResultCopy } from "../../src/lib/runResultCopy";
import { standingWords } from "../../src/lib/runState";
import { cockpitApiStub } from "../support/cockpitApi";
import { cancellableBlock, notCancellableBlock } from "../support/runV3";
import { publicReference, revisionHash as digest } from "../support/workflowV1";

/**
 * A finished run's own result lives on the node's Result tab (#666 / #716):
 * the run head is the one standing sentence, a completed run does not
 * prefetch the sink node's answer, and the Result tab renders the readable
 * form with the exact bytes behind a collapsed "Exact text" disclosure.
 * This file owns that behaviour apart from `v3RunCockpit.test.ts`, which
 * another lane's exact-scope claim holds while this fix lands.
 */

afterEach(() => cleanup());

function v3Run(overrides: Partial<RunV3> = {}): RunV3 {
  return {
    workflow_format_version: 3,
    run_id: "v3/conductor-episode",
    public_run_reference: publicReference,
    workflow_revision_hash: digest,
    agent_binding_set_hash: "b".repeat(64),
    run_configuration_revision_hash: "c".repeat(64),
    agent_bindings: [],
    orders: [],
    state_version: 1,
    state: "COMPLETED",
    current_node_id: "report",
    node_rail: [{ node_id: "report", state: "succeeded", attempt: null }],
    cancellation: notCancellableBlock("already-ended"),
    terminal_hash: "d".repeat(64),
    latest_event_cursor: null,
    started_at: "2026-08-25T15:00:00Z",
    ended_at: "2026-08-25T15:00:12Z",
    ...overrides
  };
}

function reportRevision() {
  return {
    workflow_revision_hash: digest,
    document_base64: "YQ==",
    graph: {
      workflow_format_version: 3 as const,
      executable: true as const,
      not_executable_reason: null,
      node_count: 1,
      agent_roles: ["builder"],
      orders: [],
      wait_answer_schemas: [],
      node_previews: [
        {
          id: "report",
          kind: "agent" as const,
          role: "builder",
          instruction_start: "Write the report.",
          depends_on: []
        }
      ],
      loops: [],
      name: "One report",
      description: null
    }
  };
}

function nodeDetail(overrides: Partial<NodeDetail> = {}): NodeDetail {
  return {
    run_id: "v3/conductor-episode",
    public_run_reference: publicReference,
    node_id: "report",
    state: "succeeded",
    job_base64: btoa("Report on the request."),
    job_hash: "e".repeat(64),
    answer: null,
    provenance: null,
    refusal: null,
    refusal_output: null,
    ...overrides
  };
}

/** A node whose own output a schema refused, with the exact bytes it refused (#664). */
function withRefusalOutput(raw: string, refusal: string): NodeDetail {
  return nodeDetail({
    state: "failed",
    refusal,
    refusal_output: { value_base64: btoa(raw), value_hash: "a".repeat(64) }
  });
}

/** An agent's own receipt. */
function agentProvenance(): NonNullable<NodeDetail["provenance"]> {
  return {
    role: "builder",
    provider_id: "e2e-v3",
    model: "shot-model",
    executor_revision: "immediate/v1",
    executor_operational_identity: "e2e-immediate-process",
    auth_mode: "subscription",
    profile_id: "shots",
    agent_configuration_revision_hash: "a".repeat(64),
    request_hash: "b".repeat(64),
    receipt_hash: "c".repeat(64)
  };
}

/** An agent node's declared answer, receipted the way a real one always is. */
function withAnswer(raw: string, overrides: Partial<NodeDetail> = {}): NodeDetail {
  return nodeDetail({
    answer: { value_base64: btoa(raw), value_hash: "f".repeat(64) },
    provenance: agentProvenance(),
    ...overrides
  });
}

function renderRun(run: RunV3, detail: NodeDetail) {
  const getNodeDetail = vi.fn(async () => detail);
  const cockpitApi = cockpitApiStub({
    getWorkflowRevision: vi.fn(async () => reportRevision()),
    getNodeDetail
  });
  render(V3RunView, {
    props: {
      run,
      cockpitApi,
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
  return { getNodeDetail };
}

describe("a finished run's page shows the standing sentence, not the result (#666)", () => {
  it("shows the standing sentence and no Result-named region, and does not fetch the sink until a node is opened", async () => {
    const raw = '{"answer":"The workflow could not be started: format not executable.","started_run_ids":[]}';
    const { getNodeDetail } = renderRun(v3Run(), withAnswer(raw));

    const standing = await screen.findByLabelText("Where this run stands");
    expect(standing.textContent).toContain(standingWords.done);
    await screen.findByRole("button", { name: "report — Done" });

    expect(screen.queryByRole("region", { name: runPageCopy.tabResult })).toBeNull();
    expect(screen.queryByText("The workflow could not be started: format not executable.")).toBeNull();
    expect(getNodeDetail).not.toHaveBeenCalled();
  });

  it("opening the sink node shows the decoded sentence and the collapsed Exact text disclosure", async () => {
    const raw = '{"answer":"Reviewed the diff.","started_run_ids":["run1.ZHJhZnQ"]}';
    const { getNodeDetail } = renderRun(v3Run(), withAnswer(raw));

    await fireEvent.click(await screen.findByRole("button", { name: "report — Done" }));

    const panel = await screen.findByRole("complementary");
    expect(within(panel).getByText("Reviewed the diff.", { exact: true }).isConnected).toBe(true);
    expect(within(panel).getByText("started_run_ids").isConnected).toBe(true);
    expect(within(panel).getByText("run1.ZHJhZnQ").isConnected).toBe(true);
    const disclosure = within(panel)
      .getByText(runResultCopy.exactText, { selector: "summary" })
      .closest("details");
    expect(disclosure?.open).toBe(false);
    expect(screen.queryByRole("region", { name: runPageCopy.tabResult })).toBeNull();
    expect(screen.queryByRole("link", { name: "Shown above" })).toBeNull();
    expect(getNodeDetail).toHaveBeenCalledTimes(1);
    expect(getNodeDetail).toHaveBeenCalledWith(publicReference, "report");
  });

  it("does not fetch node detail while the run is still going", async () => {
    const { getNodeDetail } = renderRun(
      v3Run({
        state: "STARTED",
        cancellation: cancellableBlock(),
        ended_at: null,
        terminal_hash: null,
        node_rail: [{ node_id: "report", state: "working", attempt: null }]
      }),
      withAnswer('{"answer":"Still writing."}')
    );

    await screen.findByLabelText("Where this run stands");
    await screen.findByRole("button", { name: "report — Working" });
    expect(screen.queryByRole("region", { name: runPageCopy.tabResult })).toBeNull();
    expect(getNodeDetail).not.toHaveBeenCalled();
  });
});

describe("the node panel's Result tab renders the same readable form (#716)", () => {
  const runEvidence = {
    runId: "v3/conductor-episode",
    workflowRevisionHash: digest,
    runConfigurationRevisionHash: "c".repeat(64),
    terminalHash: null
  };

  it("shows the declared answer sentence with the exact JSON behind a collapsed disclosure", async () => {
    const raw = '{"answer":"Reviewed the diff.","started_run_ids":["run1.ZHJhZnQ"]}';
    render(NodeDetailPanel, {
      props: {
        detail: withAnswer(raw),
        onClose: () => {},
        runEvidence
      }
    });

    expect(screen.getByText("Reviewed the diff.", { exact: true }).isConnected).toBe(true);
    expect(screen.getByText("started_run_ids").isConnected).toBe(true);
    expect(screen.getByText("run1.ZHJhZnQ").isConnected).toBe(true);
    const disclosure = screen.getByText(runResultCopy.exactText, { selector: "summary" }).closest("details");
    expect(disclosure?.open).toBe(false);

    await fireEvent.click(screen.getByText(runResultCopy.exactText, { selector: "summary" }));
    expect(disclosure?.open).toBe(true);
    expect(screen.getByText(raw).isConnected).toBe(true);
  });

  it("shows a bare string answer as itself, with no disclosure to open", async () => {
    render(NodeDetailPanel, {
      props: {
        detail: withAnswer("Three German sentences about code review."),
        onClose: () => {},
        runEvidence
      }
    });

    expect(screen.getByText("Three German sentences about code review.").isConnected).toBe(true);
    expect(screen.queryByText(runResultCopy.exactText, { selector: "summary" })).toBeNull();
  });
});

describe("the node panel shows a schema-refused answer's raw bytes and hash (#664)", () => {
  const runEvidence = {
    runId: "v3/conductor-episode",
    workflowRevisionHash: digest,
    runConfigurationRevisionHash: "c".repeat(64),
    terminalHash: null
  };
  const REFUSAL = "output-schema-refused: instance-not-json: Expecting value";
  const REFUSED_PROSE =
    "Sure! Here is what I would do: first look at the board, then start a run.";

  it("proves(a-refused-episode-shows-its-raw-output-and-hash-in-the-node-panel): shows the refused bytes under the stopped-here sentence on the Result tab, named as a redacted presentation", () => {
    render(NodeDetailPanel, {
      props: {
        detail: withRefusalOutput(REFUSED_PROSE, REFUSAL),
        onClose: () => {},
        runEvidence
      }
    });

    expect(screen.getByText("Stopped here:").isConnected).toBe(true);
    expect(screen.getByText(REFUSED_PROSE).isConnected).toBe(true);
    // The review finding this closes: the panel must say this is a redacted
    // presentation, never claim the raw bytes are shown exactly.
    expect(
      screen.getByText(runPageCopy.refusedOutputRedactionNotice).isConnected
    ).toBe(true);
  });

  it("renders whatever the server already redacted, faithfully and without a second decode", () => {
    // The server-side redaction canary (tests/integration/test_node_detail.py)
    // proves credential shapes never leave the store unredacted; this proves
    // the panel is a faithful window onto whatever text it is handed, marker
    // included, rather than a second place that could reintroduce a secret.
    const alreadyRedacted =
      "Sure, here is the token you asked for: [redacted]";
    render(NodeDetailPanel, {
      props: {
        detail: withRefusalOutput(alreadyRedacted, REFUSAL),
        onClose: () => {},
        runEvidence
      }
    });

    expect(screen.getByText(alreadyRedacted).isConnected).toBe(true);
  });

  it("lists the refused output's hash as a proof anchor on the Evidence tab", async () => {
    render(NodeDetailPanel, {
      props: {
        detail: withRefusalOutput(REFUSED_PROSE, REFUSAL),
        onClose: () => {},
        runEvidence
      }
    });

    await fireEvent.click(screen.getByRole("tab", { name: runPageCopy.tabEvidence }));

    expect(screen.getByRole("group", { name: runPageCopy.refusedOutputHash }).isConnected).toBe(
      true
    );
  });

  it("shows no refused-output block or proof anchor when a refusal names nothing to resolve", async () => {
    render(NodeDetailPanel, {
      props: {
        detail: nodeDetail({ state: "failed", refusal: REFUSAL }),
        onClose: () => {},
        runEvidence
      }
    });

    expect(screen.queryByRole("region", { name: runPageCopy.refusedOutput })).toBeNull();

    await fireEvent.click(screen.getByRole("tab", { name: runPageCopy.tabEvidence }));

    expect(screen.queryByRole("group", { name: runPageCopy.refusedOutputHash })).toBeNull();
  });

  it("bounds refusal_output.value_base64 to the wire's own agent-output-cap mirror, and admits a value exactly at that bound", () => {
    // The review finding this closes: this field carries only a V3 agent
    // node's own schema-refused output (#664), never the unrelated, larger
    // payloads the general `answer` field must also serve -- so the strict
    // Zod mirror refuses a value the server's own resource could never send.
    const atBound = withRefusalOutput(REFUSED_PROSE, REFUSAL);
    const atBoundEncoded = "a".repeat(MAXIMUM_REFUSED_OUTPUT_BASE64_CHARACTERS);
    const oversized = {
      ...atBound,
      refusal_output: { ...atBound.refusal_output, value_base64: atBoundEncoded + "a" }
    };

    expect(() =>
      nodeDetailSchema.parse({
        ...atBound,
        refusal_output: { ...atBound.refusal_output, value_base64: atBoundEncoded }
      })
    ).not.toThrow();
    expect(() => nodeDetailSchema.parse(oversized)).toThrow();
  });
});

describe("V3AnswerCard renders a predecessor's declared answer through the same reader (#716)", () => {
  it("shows the answer sentence and its remaining field inside the answer-context region, exact JSON collapsed", () => {
    const raw = '{"answer":"Reviewed the diff.","started_run_ids":["run1.a"]}';
    render(V3AnswerCard, {
      props: {
        question: "Merge this, or name the blocking defect.",
        questionMissing: false,
        sources: [{ nodeId: "review", text: raw }],
        pending: null,
        pendingAnswer: null,
        onAnswer: () => {},
        onRetry: () => {},
        onDiscard: () => {}
      }
    });

    const context = screen.getByRole("region", { name: runPageCopy.answerContext });
    expect(within(context).getByText("Reviewed the diff.", { exact: true }).isConnected).toBe(true);
    expect(within(context).getByText("started_run_ids").isConnected).toBe(true);
    expect(within(context).getByText("run1.a").isConnected).toBe(true);
    const disclosure = within(context)
      .getByText(runResultCopy.exactText, { selector: "summary" })
      .closest("details");
    expect(disclosure?.open).toBe(false);
  });
});
