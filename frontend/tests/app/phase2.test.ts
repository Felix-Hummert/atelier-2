import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import {
  CockpitRequestError,
  createCockpitApi,
  type CockpitApi,
  type Run,
  type WorkflowRevisionDetail
} from "../../src/api/client";
import { MutationJournal } from "../../src/lib/mutationJournal";

const revisionHash = "a".repeat(64);
const publicReference = "run1.cnVuLWRyYWZ0";

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

  it("does not leave a saved-workflow start armed after switching to publication", async () => {
    window.history.replaceState(null, "", "/atelier/new");
    render(App, {
      props: { cockpitApi: api(), mutationJournal: new MutationJournal(sessionStorage) }
    });
    await fireEvent.click(await screen.findByLabelText(revisionHash));
    expect(screen.getByRole("button", { name: "Start" }).isConnected).toBe(true);

    await fireEvent.click(screen.getByLabelText("Publish YAML"));

    expect(screen.queryByRole("button", { name: "Start" })).toBeNull();
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
    start: vi.fn(async () => ({ status: 201, value: run() })),
    getRun: vi.fn(async () => run()),
    getWorkflowRevision: vi.fn(async () => revision()),
    openRunEvents: vi.fn(() => ({ close: vi.fn() })),
    ...overrides
  };
}

function run(): Run {
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
