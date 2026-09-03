import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AttemptTranscript, NodeDetail } from "../../src/api/client";
import NodeDetailPanel from "../../src/components/NodeDetailPanel.svelte";
import V3RunView from "../../src/components/V3RunView.svelte";
import { wrapDisplayCopy } from "../../src/lib/displayCopy";
import { MutationJournal } from "../../src/lib/mutationJournal";
import {
  runPageCopy,
  transcriptDroppedCopy,
  usageLine
} from "../../src/lib/runPageCopy";
import { cockpitApiStub } from "../support/cockpitApi";
import { notCancellableBlock } from "../support/runV3";
import { publicReference, revisionHash as digest } from "../support/runV3";

afterEach(() => {
  cleanup();
  window.history.replaceState(null, "", "/atelier");
});

const runEvidence = {
  runId: "v3/log-tab",
  workflowRevisionHash: digest,
  runConfigurationRevisionHash: "c".repeat(64),
  terminalHash: "d".repeat(64)
};

const PLANTED_CANARY = "sk-ant" + "-plantedcanarysecret0123456789";
const beforeMoments = { origin: "v1-before-moments" as const };

function nodeDetail(overrides: Partial<NodeDetail> = {}): NodeDetail {
  return {
    run_id: "v3/log-tab",
    public_run_reference: publicReference,
    node_id: "reviewer",
    state: "succeeded",
    job_base64: btoa("Check the changed files."),
    job_hash: "e".repeat(64),
    answer: { value_base64: btoa("The files still disagree."), value_hash: "f".repeat(64) },
    provenance: null,
    refusal: null,
    refusal_output: null,
    started_at: "2026-08-18T15:00:00Z",
    ended_at: "2026-08-18T15:00:10Z",
    ...overrides
  };
}

function sequenceTranscript(): AttemptTranscript {
  return {
    events: [
      {
        event: "assistant-turn",
        text: "I will check the changed files against the review brief.",
        redacted: false,
        moment: beforeMoments
      },
      {
        event: "tool-called",
        name: "Read",
        arguments: '{"path":"docs/requirements/0003-ziel-ui.md"}',
        redacted: false,
        moment: beforeMoments
      },
      {
        event: "tool-returned",
        name: "Read",
        result: "Read 128 lines.",
        redacted: false,
        moment: beforeMoments
      },
      {
        event: "usage",
        input_tokens: 12_400,
        output_tokens: 680,
        cache_read_input_tokens: 0,
        cache_creation_input_tokens: 0,
        moment: beforeMoments
      }
    ]
  };
}

async function openLog(detail: NodeDetail): Promise<HTMLElement> {
  render(NodeDetailPanel, {
    props: { detail, onClose: () => {}, runEvidence }
  });
  await fireEvent.click(screen.getByRole("tab", { name: wrapDisplayCopy(runPageCopy.tabLog) }));
  return screen.getByRole("region", { name: wrapDisplayCopy(runPageCopy.transcriptRegion) });
}

describe("the node panel Log tab renders the stored attempt transcript (#666)", () => {
  it("shows turns, folded door calls, answers, and a usage line from the event", async () => {
    const region = await openLog(nodeDetail({ transcript: sequenceTranscript() }));

    expect(within(region).getByText(runPageCopy.assistantTurn).isConnected).toBe(true);
    expect(
      within(region).getByText("I will check the changed files against the review brief.").isConnected
    ).toBe(true);
    expect(within(region).getByText(runPageCopy.doorCall).isConnected).toBe(true);
    expect(within(region).getByText("Read").isConnected).toBe(true);
    expect(within(region).getByText(runPageCopy.argumentsFold).isConnected).toBe(true);
    const argumentsBox = within(region).getByText('{"path":"docs/requirements/0003-ziel-ui.md"}');
    expect(argumentsBox.closest("details")?.open).toBe(false);

    await fireEvent.click(within(region).getByText(runPageCopy.argumentsFold));
    expect(argumentsBox.closest("details")?.open).toBe(true);
    expect(argumentsBox.isConnected).toBe(true);

    expect(within(region).getByText(runPageCopy.doorAnswer).isConnected).toBe(true);
    expect(within(region).getByText("Read 128 lines.").isConnected).toBe(true);
    expect(within(region).getByText(runPageCopy.usage).isConnected).toBe(true);
    expect(within(region).getByText(usageLine(12_400, 680, "10 s")).isConnected).toBe(true);
    expect(within(region).queryByText(/cache/i)).toBeNull();
    expect(region.querySelectorAll("time")).toHaveLength(1);
  });

  it("renders the redaction badge in place of the marker and never a planted canary", async () => {
    const region = await openLog(
      nodeDetail({
        transcript: {
          events: [
            {
              event: "assistant-turn",
              text: "canary credential: [redacted]",
              redacted: true,
              moment: beforeMoments
            },
            {
              event: "assistant-turn",
              text: "nothing secret remains in this turn",
              redacted: true,
              moment: beforeMoments
            }
          ]
        }
      })
    );

    expect(within(region).getAllByText(runPageCopy.redacted).length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText(PLANTED_CANARY)).toBeNull();
    expect(region.textContent).not.toContain(PLANTED_CANARY);
    expect(within(region).getByText(/canary credential:/).isConnected).toBe(true);
    expect(within(region).getByText("nothing secret remains in this turn").isConnected).toBe(true);
  });

  it("shows a failed attempt's stdout as Attempt stdout with Failed and no canary", async () => {
    const region = await openLog(
      nodeDetail({
        state: "failed",
        answer: null,
        refusal: "The last attempt stopped while checking the changed files.",
        transcript: {
          events: [
            {
              event: "unrecognised-provider-output",
              text: "checking changed files\ncanary credential: [redacted]\ncommand stopped before an answer",
              redacted: true,
              moment: beforeMoments
            }
          ]
        }
      })
    );

    expect(within(region).getByText(runPageCopy.attemptStdout).isConnected).toBe(true);
    expect(within(region).getByText("Failed").isConnected).toBe(true);
    expect(within(region).getByText(runPageCopy.redacted).isConnected).toBe(true);
    expect(screen.queryByText(PLANTED_CANARY)).toBeNull();
    expect(region.textContent).not.toContain(PLANTED_CANARY);
    expect(within(region).getByText(/checking changed files/).isConnected).toBe(true);
  });

  it("names absence with the hollow empty state on an ended node, never the lease paragraph", async () => {
    render(NodeDetailPanel, {
      props: { detail: nodeDetail({ transcript: null }), onClose: () => {}, runEvidence }
    });
    await fireEvent.click(screen.getByRole("tab", { name: wrapDisplayCopy(runPageCopy.tabLog) }));

    expect(screen.getByRole("status").textContent).toContain(runPageCopy.transcriptEmpty);
    expect(screen.queryByText(runPageCopy.processLogInLease)).toBeNull();
    expect(screen.queryByText(runPageCopy.logAbsent)).toBeNull();
    expect(screen.queryByRole("region", { name: runPageCopy.transcriptRegion })).toBeNull();
  });

  it("shows Looking… as status, never a progressbar, while detail is in flight", async () => {
    render(NodeDetailPanel, {
      props: {
        detail: null,
        nodeId: "reviewer",
        railState: "failed",
        onClose: () => {},
        runEvidence
      }
    });

    expect(screen.getByRole("heading", { name: "reviewer" }).isConnected).toBe(true);
    await fireEvent.click(screen.getByRole("tab", { name: wrapDisplayCopy(runPageCopy.tabLog) }));

    expect(screen.getByRole("status").textContent).toContain(runPageCopy.questionLooking);
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(screen.queryByText(runPageCopy.processLogInLease)).toBeNull();
  });

  it("keeps the live process log in the lease on a working node with no stored transcript", async () => {
    render(NodeDetailPanel, {
      props: {
        detail: nodeDetail({
          state: "working",
          answer: null,
          started_at: "2026-08-18T15:00:00Z",
          ended_at: null,
          transcript: null
        }),
        onClose: () => {},
        runEvidence
      }
    });
    await fireEvent.click(screen.getByRole("tab", { name: wrapDisplayCopy(runPageCopy.tabLog) }));

    expect(screen.getByText(runPageCopy.processLogInLease).isConnected).toBe(true);
    expect(screen.getByText(runPageCopy.logAbsent).isConnected).toBe(true);
    expect(screen.queryByText(runPageCopy.transcriptEmpty)).toBeNull();
  });

  it("reads kind names and empty copy from the owner under a pseudo-locale", async () => {
    window.history.replaceState(null, "", "/atelier?pseudo-locale=1");
    const region = await openLog(nodeDetail({ transcript: sequenceTranscript() }));

    expect(within(region).getByText(wrapDisplayCopy(runPageCopy.assistantTurn)).isConnected).toBe(
      true
    );
    expect(within(region).getByText(wrapDisplayCopy(runPageCopy.doorCall)).isConnected).toBe(true);
    expect(within(region).getByText(wrapDisplayCopy(runPageCopy.doorAnswer)).isConnected).toBe(true);
    expect(within(region).getByText(wrapDisplayCopy(runPageCopy.usage)).isConnected).toBe(true);
    expect(
      within(region).getByText(wrapDisplayCopy(usageLine(12_400, 680, "10 s"))).isConnected
    ).toBe(true);
  });

  it("moves between tabs with arrows and activates one with Enter or Space", async () => {
    render(NodeDetailPanel, {
      props: { detail: nodeDetail({ transcript: sequenceTranscript() }), onClose: () => {}, runEvidence }
    });
    const tablist = screen.getByRole("tablist", { name: wrapDisplayCopy(runPageCopy.tabsLabel) });
    const resultTab = within(tablist).getByRole("tab", { name: wrapDisplayCopy(runPageCopy.tabResult) });
    const inputTab = within(tablist).getByRole("tab", { name: wrapDisplayCopy(runPageCopy.tabInput) });
    const logTab = within(tablist).getByRole("tab", { name: wrapDisplayCopy(runPageCopy.tabLog) });
    const evidenceTab = within(tablist).getByRole("tab", {
      name: wrapDisplayCopy(runPageCopy.tabEvidence)
    });

    expect(resultTab.getAttribute("aria-selected")).toBe("true");
    expect(resultTab.tabIndex).toBe(0);
    expect(inputTab.tabIndex).toBe(-1);

    resultTab.focus();
    expect(document.activeElement).toBe(resultTab);
    await fireEvent.keyDown(resultTab, { key: "Tab" });
    expect(document.activeElement).toBe(resultTab);
    expect(inputTab.tabIndex).toBe(-1);

    await fireEvent.keyDown(resultTab, { key: "ArrowRight" });
    await waitFor(() => expect(inputTab.getAttribute("aria-selected")).toBe("true"));
    expect(inputTab.tabIndex).toBe(0);
    expect(resultTab.tabIndex).toBe(-1);

    logTab.focus();
    await fireEvent.keyDown(logTab, { key: "Enter" });
    await waitFor(() => expect(logTab.getAttribute("aria-selected")).toBe("true"));
    expect(
      screen.getByRole("region", { name: wrapDisplayCopy(runPageCopy.transcriptRegion) }).isConnected
    ).toBe(true);

    await fireEvent.keyDown(logTab, { key: "End" });
    await waitFor(() => expect(evidenceTab.getAttribute("aria-selected")).toBe("true"));

    await fireEvent.keyDown(evidenceTab, { key: "Home" });
    await waitFor(() => expect(resultTab.getAttribute("aria-selected")).toBe("true"));

    await fireEvent.keyDown(resultTab, { key: " " });
    expect(resultTab.getAttribute("aria-selected")).toBe("true");
  });

  it("opens and closes a door-call fold with Enter and Space", async () => {
    const region = await openLog(nodeDetail({ transcript: sequenceTranscript() }));
    const fold = within(region).getByText(runPageCopy.argumentsFold);
    const summary = fold.closest("summary");
    const details = fold.closest("details");
    expect(summary).not.toBeNull();
    expect(details).not.toBeNull();
    expect(details?.open).toBe(false);

    summary?.focus();
    expect(document.activeElement).toBe(summary);
    await fireEvent.keyDown(summary as HTMLElement, { key: "Enter" });
    if (details !== null && !details.open) {
      await fireEvent.click(summary as HTMLElement);
    }
    expect(details?.open).toBe(true);
    expect(within(region).getByText('{"path":"docs/requirements/0003-ziel-ui.md"}').isConnected).toBe(
      true
    );

    await fireEvent.keyDown(summary as HTMLElement, { key: " " });
    if (details !== null && details.open) {
      await fireEvent.click(summary as HTMLElement);
    }
    expect(details?.open).toBe(false);
  });

  it("names how many events the stored transcript dropped", async () => {
    const region = await openLog(
      nodeDetail({
        transcript: {
          events: [
            {
              event: "assistant-turn",
              text: "I will check the changed files.",
              redacted: false,
              moment: beforeMoments
            },
            { event: "transcript-truncated", dropped_events: 4, moment: beforeMoments }
          ]
        }
      })
    );

    expect(within(region).getByText(transcriptDroppedCopy(4)).isConnected).toBe(true);
  });
});

describe("the run view shows panel chrome while node detail is still arriving", () => {
  it("keeps the node id and tabs, and the Log tab's Looking… status, until the node is read", async () => {
    const cockpitApi = cockpitApiStub({
      getWorkflowRevision: vi.fn(async () => ({
        workflow_revision_hash: digest,
        document_base64: "YQ==",
        provenance: null,
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
              id: "reviewer",
              kind: "agent" as const,
              role: "builder",
              instruction_start: "Check what the node before you did.",
              depends_on: []
            }
          ],
          loops: [],
          name: "One reviewer",
          description: null
        }
      })),
      getNodeDetail: vi.fn(() => new Promise<NodeDetail>(() => undefined))
    });

    render(V3RunView, {
      props: {
        run: {
          workflow_format_version: 3,
          run_id: "v3/log-tab",
          public_run_reference: publicReference,
          workflow_revision_hash: digest,
          agent_binding_set_hash: "b".repeat(64),
          run_configuration_revision_hash: "c".repeat(64),
          agent_bindings: [],
          orders: [],
          state_version: 1,
          state: "FAILED",
          current_node_id: "reviewer",
          current_node_execution_id: digest,
          node_rail: [{ node_id: "reviewer", state: "failed", attempt: null }],
          cancellation: notCancellableBlock("already-ended"),
          terminal_hash: "d".repeat(64),
          latest_event_cursor: null,
          started_at: "2026-08-18T15:00:00Z",
          ended_at: "2026-08-18T15:00:10Z"
        },
        cockpitApi,
        mutationJournal: new MutationJournal(sessionStorage)
      }
    });

    await screen.findByRole("heading", { level: 1, name: "One reviewer" });

    const panel = await screen.findByRole("complementary");
    expect(within(panel).getByRole("heading", { name: "reviewer" }).isConnected).toBe(true);
    expect(within(panel).getByRole("tablist", { name: runPageCopy.tabsLabel }).isConnected).toBe(
      true
    );
    expect(screen.queryByText(/Reading reviewer/)).toBeNull();

    await fireEvent.click(within(panel).getByRole("tab", { name: runPageCopy.tabLog }));
    expect(within(panel).getByRole("status").textContent).toContain(runPageCopy.questionLooking);
    expect(screen.queryByRole("progressbar")).toBeNull();
  });
});
