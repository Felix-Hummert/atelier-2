import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import ReadState from "../../src/components/ReadState.svelte";
import { reportConnectionLost, reportConnectionRestored } from "../../src/lib/connectionState";
import {
  beginRead,
  confirmRead,
  failRead,
  retainedRead,
  type RetainedRead
} from "../../src/lib/readResource";

type ReadStateFailure =
  | { kind: "unavailable"; title: string }
  | { kind: "incomplete"; title: string };

afterEach(() => {
  cleanup();
  reportConnectionRestored();
});

describe("recoverable read state", () => {
  it("suppresses its own failure and Retry while the whole workshop reads unreachable, and shows them again once it does not (#700)", async () => {
    const first = beginRead(retainedRead<string, ReadStateFailure>());
    const failed = failRead(
      first.read,
      first.generation,
      { kind: "unavailable", title: "Saved workflows unavailable" }
    );
    render(ReadState, { props: { read: failed, label: "saved workflows", onRetry: vi.fn() } });
    expect(screen.getByRole("alert").isConnected).toBe(true);

    reportConnectionLost();
    await waitFor(() => {
      expect(screen.queryByRole("alert")).toBeNull();
      expect(screen.queryByRole("button", { name: "Retry saved workflows" })).toBeNull();
    });

    reportConnectionRestored();
    await waitFor(() => {
      expect(screen.getByText("Saved workflows unavailable").isConnected).toBe(true);
      expect(screen.getByRole("button", { name: "Retry saved workflows" }).isConnected).toBe(true);
    });
  });

  it("offers a Retry control only while the read is failed, and Retry repeats that read", async () => {
    const retry = vi.fn();
    const first = beginRead(retainedRead<string, ReadStateFailure>());
    const failed = failRead(
      first.read,
      first.generation,
      { kind: "unavailable", title: "Saved workflows unavailable" }
    );
    render(ReadState, { props: { read: failed, label: "saved workflows", onRetry: retry } });

    const button = screen.getByRole("button", { name: "Retry saved workflows" });
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(screen.getByRole("alert").textContent).toContain("Saved workflows unavailable");
    expect(screen.getByRole("alert").textContent).not.toContain("Failed to fetch");

    await fireEvent.click(button);
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it("does not steal focus on a first, unprompted failure, but returns it to Retry when the operator's own retry fails again", async () => {
    const first = beginRead(retainedRead<string, ReadStateFailure>());
    const failed = failRead(
      first.read,
      first.generation,
      { kind: "unavailable", title: "Saved workflows unavailable" }
    );
    const view = render(ReadState, {
      props: { read: failed, label: "saved workflows", onRetry: vi.fn() }
    });
    expect(document.activeElement).not.toBe(
      screen.getByRole("button", { name: "Retry saved workflows" })
    );

    await fireEvent.click(screen.getByRole("button", { name: "Retry saved workflows" }));
    const secondAttempt = beginRead(failed);
    await view.rerender({ read: secondAttempt.read, label: "saved workflows", onRetry: vi.fn() });
    expect(screen.queryByRole("button")).toBeNull();

    const retriedAgain = failRead(
      secondAttempt.read,
      secondAttempt.generation,
      { kind: "unavailable", title: "Saved workflows unavailable" }
    );
    await view.rerender({ read: retriedAgain, label: "saved workflows", onRetry: vi.fn() });

    expect(document.activeElement).toBe(
      screen.getByRole("button", { name: "Retry saved workflows" })
    );
  });

  it("shows no button while looking, refreshing or holding confirmed truth -- a control only ever answers a named failure", () => {
    const first = beginRead(retainedRead<string, ReadStateFailure>());
    const looking = first.read;
    render(ReadState, { props: { read: looking, label: "saved workflows", onRetry: vi.fn() } });
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByRole("status").textContent).toContain("Looking…");
    cleanup();

    const confirmed = confirmRead(looking, first.generation, "truth");
    const refreshing = beginRead(confirmed).read;
    render(ReadState, { props: { read: refreshing, label: "saved workflows", onRetry: vi.fn() } });
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByRole("status").textContent).toContain("Refreshing…");
    cleanup();

    const idleConfirmed: RetainedRead<string, ReadStateFailure> = {
      confirmed: "truth",
      generation: confirmed.generation,
      request: { state: "idle" }
    };
    render(ReadState, { props: { read: idleConfirmed, label: "saved workflows", onRetry: vi.fn() } });
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
  });
});
