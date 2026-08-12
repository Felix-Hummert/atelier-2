import { describe, expect, it, vi } from "vitest";

import { createCockpitApi, type RunEventHandlers } from "../../src/api/client";

describe("native durable event transport", () => {
  it("opens the exact same-origin event route and forwards every named R2 event", () => {
    const source = new FakeEventSource();
    const factory = vi.fn(() => source);
    const handlers: RunEventHandlers = {
      opened: vi.fn(),
      event: vi.fn(),
      disconnected: vi.fn()
    };
    const api = createCockpitApi(fetch, factory);

    const subscription = api.openRunEvents("run1.cnVu", handlers);
    source.dispatch("open", new Event("open"));
    for (const name of eventNames) {
      source.dispatch(name, message(name));
    }
    source.dispatch("error", new Event("error"));

    expect(factory).toHaveBeenCalledWith("/atelier/api/v1/runs/run1.cnVu/events");
    expect(handlers.opened).toHaveBeenCalledTimes(1);
    expect(handlers.event).toHaveBeenCalledTimes(eventNames.length);
    expect(handlers.event).toHaveBeenLastCalledWith(JSON.stringify({ event: "SUBWORKFLOW_COMPLETED" }));
    expect(handlers.disconnected).toHaveBeenCalledTimes(1);
    subscription.close();
    expect(source.closed).toBe(true);
  });

  it("binds a fetched workflow projection to the exact requested revision", async () => {
    const requested = "a".repeat(64);
    const other = "b".repeat(64);
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({
        revision_hash: other,
        document_base64: "",
        graph: {
          format_version: 1,
          start_node_id: "final",
          nodes: [
            {
              type: "subworkflow",
              node_id: "final",
              operation: "add",
              operands: [2, 3],
              next_node_id: null
            }
          ]
        }
      })
    );

    await expect(createCockpitApi(fetcher).getWorkflowRevision(requested)).rejects.toThrow(
      "did not match the requested revision"
    );
    expect(fetcher).toHaveBeenCalledWith(
      `/atelier/api/v1/workflow-revisions/${requested}`,
      expect.any(Object)
    );
  });
});

const eventNames = [
  "AGENT_COMPLETED",
  "ACTION_RECONCILIATION_REQUIRED",
  "ACTION_RECONCILIATION_RESOLVED",
  "ACTION_COMPLETED",
  "WAITING_INPUT",
  "WAIT_ANSWERED",
  "SUBWORKFLOW_COMPLETED"
] as const;

function message(event: string): MessageEvent<string> {
  return new MessageEvent("message", { data: JSON.stringify({ event }) });
}

class FakeEventSource {
  closed = false;
  private readonly listeners = new Map<string, EventListener[]>();

  addEventListener(type: string, listener: EventListener): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }

  close(): void {
    this.closed = true;
  }

  dispatch(type: string, event: Event): void {
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "content-type": "application/json" }
  });
}
