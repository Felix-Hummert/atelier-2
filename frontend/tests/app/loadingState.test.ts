import { cleanup, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it } from "vitest";

import LoadingState from "../../src/components/LoadingState.svelte";
import { readStateCopy } from "../../src/lib/readStateCopy";

afterEach(() => {
  cleanup();
});

/**
 * REQ-UIQ-10's silent skeleton, at its one owner: a shape mark and shape
 * lines carry the signal, the label is the accessible sentence beside it --
 * never a spinner glyph, never dimmed prose standing alone.
 */
describe("the shared loading state", () => {
  it("draws the skeleton mark and two lines by default, with the label as an accessible status", () => {
    const { container } = render(LoadingState, { props: { label: "Looking…" } });
    const status = screen.getByRole("status");
    expect(status.textContent).toContain("Looking…");
    expect(container.querySelectorAll(".loading-mark")).toHaveLength(1);
    expect(container.querySelectorAll(".loading-lines > span")).toHaveLength(2);
    expect(status.textContent).not.toContain("↻");
  });

  it("draws one line and carries the compact class the CSS keys its own chrome-free, fixed-width bar off of", () => {
    const { container } = render(LoadingState, { props: { label: "Looking…", compact: true } });
    expect(container.querySelectorAll(".loading-mark")).toHaveLength(1);
    expect(container.querySelectorAll(".loading-lines > span")).toHaveLength(1);
    // jsdom draws no layout, so the border-removal and fixed-width rules at
    // LoadingState.svelte's `.loading-state.compact` and
    // `.loading-state.compact .loading-lines` cannot be asserted here as
    // computed style; this pins the one thing a unit test can -- that the
    // class those rules key off of is actually present on the rendered
    // root. The real-browser screenshots taken for issue #938 are the
    // proof that the CSS itself renders a visible bar at both widths.
    expect(container.querySelector(".loading-state.compact")).not.toBeNull();
  });

  it("defaults its label to the copy owner's looking sentence", () => {
    render(LoadingState, { props: {} });
    expect(screen.getByRole("status").textContent).toContain(readStateCopy.looking);
  });
});
