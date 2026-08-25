import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CockpitApi, NodeDetail, RunV3 } from "../../src/api/client";
import NodeDetailPanel from "../../src/components/NodeDetailPanel.svelte";
import V3AnswerCard from "../../src/components/V3AnswerCard.svelte";
import V3RunView from "../../src/components/V3RunView.svelte";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { runPageCopy } from "../../src/lib/runPageCopy";
import { runResultCopy } from "../../src/lib/runResultCopy";
import { cockpitApiStub } from "../support/cockpitApi";
import { cancellableBlock, notCancellableBlock } from "../support/runV3";
import { publicReference, revisionHash as digest } from "../support/workflowV1";

/**
 * A finished run's own result reads as prose everywhere it appears (#716):
 * the run page shows its sink node's answer without a click, and the node
 * panel's Result tab renders the identical readable form, the exact bytes
 * kept only behind a collapsed "Exact text" disclosure. This file owns that
 * behaviour apart from `v3RunCockpit.test.ts`, which another lane's
 * exact-scope claim holds while this fix lands.
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
    ...overrides
  };
}

/** An agent's own receipt (#716's outcome banner only ever names an agent's report). */
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

describe("a finished run's page shows its own result, unclicked (#716)", () => {
  it("renders a declared object's answer field as one plain sentence above the graph", async () => {
    const raw = '{"answer":"The workflow could not be started: format not executable.","started_run_ids":[]}';
    const cockpitApi = cockpitApiStub({
      getNodeDetail: vi.fn(async () => withAnswer(raw))
    });

    render(V3RunView, {
      props: {
        run: v3Run(),
        cockpitApi,
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    const outcome = await screen.findByRole("region", { name: runPageCopy.tabResult });
    expect(
      within(outcome).getByText("The workflow could not be started: format not executable.", {
        exact: true
      }).isConnected
    ).toBe(true);
    // Never a raw JSON line open on the main surface -- the exact bytes stay
    // behind a disclosure the operator has not opened.
    expect(within(outcome).getByText(raw).closest("details")?.open).toBe(false);
  });

  it("shows a remaining non-empty field after the answer sentence -- nothing material only in the disclosure", async () => {
    const raw = '{"answer":"Started the fix.","started_run_ids":["run1.a"]}';
    const cockpitApi = cockpitApiStub({
      getNodeDetail: vi.fn(async () => withAnswer(raw))
    });

    render(V3RunView, {
      props: {
        run: v3Run(),
        cockpitApi,
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    const outcome = await screen.findByRole("region", { name: runPageCopy.tabResult });
    expect(within(outcome).getByText("Started the fix.", { exact: true }).isConnected).toBe(true);
    expect(within(outcome).getByText("started_run_ids").isConnected).toBe(true);
    expect(within(outcome).getByText("run1.a").isConnected).toBe(true);
  });

  it("renders a declared object with no answer field as its named fields", async () => {
    const raw = '{"verdict":"green","findings":2}';
    const cockpitApi = cockpitApiStub({
      getNodeDetail: vi.fn(async () => withAnswer(raw))
    });

    render(V3RunView, {
      props: {
        run: v3Run(),
        cockpitApi,
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    const outcome = await screen.findByRole("region", { name: runPageCopy.tabResult });
    expect(within(outcome).getByText("verdict").isConnected).toBe(true);
    expect(within(outcome).getByText("green").isConnected).toBe(true);
    expect(within(outcome).getByText("2").isConnected).toBe(true);
  });

  it("renders a declared array as its own items, never as a JSON line", async () => {
    const raw = '["one finding","another finding"]';
    const cockpitApi = cockpitApiStub({
      getNodeDetail: vi.fn(async () => withAnswer(raw))
    });

    render(V3RunView, {
      props: {
        run: v3Run(),
        cockpitApi,
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    const outcome = await screen.findByRole("region", { name: runPageCopy.tabResult });
    expect(within(outcome).getByText("one finding", { exact: true }).isConnected).toBe(true);
    expect(within(outcome).getByText("another finding", { exact: true }).isConnected).toBe(true);
    expect(within(outcome).getByText(raw).closest("details")?.open).toBe(false);
  });

  it("shows no outcome while the run is still going", async () => {
    const cockpitApi = cockpitApiStub({ getNodeDetail: vi.fn() });

    render(V3RunView, {
      props: {
        run: v3Run({ state: "STARTED", cancellation: cancellableBlock() }),
        cockpitApi,
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    await screen.findByLabelText("Where this run stands");
    expect(screen.queryByRole("region", { name: runPageCopy.tabResult })).toBeNull();
    expect(cockpitApi.getNodeDetail).not.toHaveBeenCalled();
  });

  it("shows no banner for a FAILED run whose node wrote no answer", async () => {
    const cockpitApi = cockpitApiStub({
      getNodeDetail: vi.fn(async () =>
        nodeDetail({ state: "failed", refusal: "output-schema-refused: instance-not-json" })
      )
    });

    render(V3RunView, {
      props: {
        run: v3Run({
          state: "FAILED",
          node_rail: [{ node_id: "report", state: "failed", attempt: null }],
          cancellation: notCancellableBlock("already-ended")
        }),
        cockpitApi,
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    await screen.findByLabelText("Where this run stands");
    await vi.waitFor(() => expect(cockpitApi.getNodeDetail).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("region", { name: runPageCopy.tabResult })).toBeNull();
  });

  it("shows no banner for a run that ended on an answered Wait node -- that answer is the operator's, not the run's own report", async () => {
    const cockpitApi = cockpitApiStub({
      // #562: an answered Wait now carries a real `answer`, but never an
      // agent `provenance` -- nothing ran it.
      getNodeDetail: vi.fn(async () => withAnswer('"approved"', { provenance: null }))
    });

    render(V3RunView, {
      props: {
        run: v3Run(),
        cockpitApi,
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    await screen.findByLabelText("Where this run stands");
    await vi.waitFor(() => expect(cockpitApi.getNodeDetail).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("region", { name: runPageCopy.tabResult })).toBeNull();
  });

  it("surfaces a failed outcome read and retries it once the run is read again", async () => {
    const getNodeDetail = vi
      .fn<CockpitApi["getNodeDetail"]>()
      .mockRejectedValueOnce(new Error("the durable store is unavailable"))
      .mockResolvedValue(withAnswer('{"answer":"Recovered."}'));
    const cockpitApi = cockpitApiStub({ getNodeDetail });
    const run = v3Run();
    const mutationJournal = new MutationJournal(sessionStorage);

    const view = render(V3RunView, { props: { run, cockpitApi, mutationJournal } });

    await screen.findByText("the durable store is unavailable");
    expect(screen.queryByRole("region", { name: runPageCopy.tabResult })).toBeNull();

    // The same run read again (e.g. after a stream event) tries the outcome
    // fetch again rather than staying silent on the first failure forever.
    await view.rerender({ run: { ...run }, cockpitApi, mutationJournal });
    await screen.findByRole("region", { name: runPageCopy.tabResult });
    expect(getNodeDetail).toHaveBeenCalledTimes(2);
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

  it("names the run page's own banner instead of rendering the sink node's answer a second time", () => {
    render(NodeDetailPanel, {
      props: {
        detail: withAnswer('{"answer":"Reviewed the diff."}'),
        onClose: () => {},
        runEvidence,
        resultShownAbove: true
      }
    });

    expect(screen.queryByText("Reviewed the diff.")).toBeNull();
    expect(screen.queryByText(runResultCopy.exactText, { selector: "summary" })).toBeNull();
    const link = screen.getByRole("link", { name: runResultCopy.shownAbove });
    expect(link.getAttribute("href")).toBe("#run-outcome");
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
