import { cleanup, fireEvent, render, screen, within } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { NodeDetail, RunV3 } from "../../src/api/client";
import NodeDetailPanel from "../../src/components/NodeDetailPanel.svelte";
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
 * panel's Result tab renders the identical readable form, the raw bytes kept
 * only behind a collapsed "Raw" disclosure. This file owns that behaviour
 * apart from `v3RunCockpit.test.ts`, which another lane's exact-scope claim
 * holds while this fix lands.
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

describe("a finished run's page shows its own result, unclicked (#716)", () => {
  it("renders a declared object's answer field as one plain sentence above the graph", async () => {
    const raw = '{"answer":"The workflow could not be started: format not executable.","started_run_ids":[]}';
    const cockpitApi = cockpitApiStub({
      getNodeDetail: vi.fn(async () =>
        nodeDetail({ answer: { value_base64: btoa(raw), value_hash: "f".repeat(64) } })
      )
    });

    render(V3RunView, {
      props: {
        run: v3Run(),
        cockpitApi,
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    const outcome = await screen.findByRole("region", { name: runPageCopy.tabResult });
    expect(within(outcome).getByText("The workflow could not be started: format not executable.").isConnected).toBe(true);
    // Never a raw JSON line open on the main surface -- the exact bytes stay
    // behind a disclosure the operator has not opened.
    expect(
      within(outcome).getByText(runResultCopy.raw, { selector: "summary" }).closest("details")?.open
    ).toBe(false);
  });

  it("renders a declared object with no answer field as its named fields", async () => {
    const raw = '{"verdict":"green","findings":2}';
    const cockpitApi = cockpitApiStub({
      getNodeDetail: vi.fn(async () =>
        nodeDetail({ answer: { value_base64: btoa(raw), value_hash: "f".repeat(64) } })
      )
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
});

describe("the node panel's Result tab renders the same readable form (#716)", () => {
  const runEvidence = {
    runId: "v3/conductor-episode",
    workflowRevisionHash: digest,
    runConfigurationRevisionHash: "c".repeat(64),
    terminalHash: null
  };

  it("shows the declared answer sentence with the raw JSON behind a collapsed Raw disclosure", async () => {
    const raw = '{"answer":"Reviewed the diff.","started_run_ids":["run1.ZHJhZnQ"]}';
    render(NodeDetailPanel, {
      props: {
        detail: nodeDetail({ answer: { value_base64: btoa(raw), value_hash: "f".repeat(64) } }),
        onClose: () => {},
        runEvidence
      }
    });

    expect(screen.getByText("Reviewed the diff.").isConnected).toBe(true);
    const disclosure = screen.getByText(runResultCopy.raw, { selector: "summary" }).closest("details");
    expect(disclosure?.open).toBe(false);

    await fireEvent.click(screen.getByText(runResultCopy.raw, { selector: "summary" }));
    expect(disclosure?.open).toBe(true);
    expect(screen.getByText(raw).isConnected).toBe(true);
  });

  it("shows a bare string answer as itself, with no Raw disclosure to open", async () => {
    render(NodeDetailPanel, {
      props: {
        detail: nodeDetail({
          answer: { value_base64: btoa("Three German sentences about code review."), value_hash: "f".repeat(64) }
        }),
        onClose: () => {},
        runEvidence
      }
    });

    expect(screen.getByText("Three German sentences about code review.").isConnected).toBe(true);
    expect(screen.queryByText(runResultCopy.raw, { selector: "summary" })).toBeNull();
  });
});
