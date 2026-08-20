import { cleanup, fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import ReadState from "../../src/components/ReadState.svelte";
import {
  beginRead,
  failRead,
  retainedRead
} from "../../src/lib/readResource";

type ReadStateFailure =
  | { kind: "unavailable"; title: string }
  | { kind: "incomplete"; title: string };

afterEach(cleanup);

describe("recoverable read state", () => {
  it("keeps one accessible control and its focus across failure, retry and success", async () => {
    const retry = vi.fn();
    const first = beginRead(retainedRead<string, ReadStateFailure>());
    const failed = failRead(
      first.read,
      first.generation,
      { kind: "unavailable", title: "Saved workflows unavailable" }
    );
    const view = render(ReadState, {
      props: { read: failed, label: "saved workflows", onRetry: retry }
    });

    const button = screen.getByRole("button", { name: "Retry saved workflows" });
    button.focus();
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(screen.getByRole("alert").textContent).toContain("Saved workflows unavailable");
    expect(screen.getByRole("alert").textContent).not.toContain("Failed to fetch");
    expect(screen.queryByText("Retry this read.")).toBeNull();

    await fireEvent.click(button);
    expect(retry).toHaveBeenCalledTimes(1);

    await view.rerender({
      read: beginRead(failed).read,
      label: "saved workflows",
      onRetry: retry
    });
    expect(screen.getByRole("button", { name: "Refresh saved workflows" })).toBe(button);
    expect(document.activeElement).toBe(button);
    expect(screen.getByRole("status").textContent).toContain("Looking…");

    await view.rerender({
      read: { ...failed, confirmed: "truth", request: { state: "idle" } },
      label: "saved workflows",
      onRetry: retry
    });
    expect(screen.getByRole("button", { name: "Refresh saved workflows" })).toBe(button);
    expect(document.activeElement).toBe(button);
  });
});
