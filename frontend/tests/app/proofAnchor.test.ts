import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import ProofAnchor from "../../src/components/ProofAnchor.svelte";
import { runPageCopy } from "../../src/lib/runPageCopy";

const value = "a".repeat(64);

afterEach(() => cleanup());

describe("the named proof affordance", () => {
  it("proves(a-run-page-hash-is-a-named-proof-anchor): the compact chip leads with the human name, not the hex", () => {
    render(ProofAnchor, {
      props: {
        label: runPageCopy.promptHash,
        seals: runPageCopy.sealsPrompt,
        value,
        compact: true
      }
    });

    const trigger = screen.getByRole("button", { name: runPageCopy.promptHash });
    expect(trigger.textContent).toBe(runPageCopy.promptHash);
    expect(trigger.textContent).not.toContain(value);
    expect(trigger.getAttribute("title")).toBeNull();
    expect(screen.queryByText(value)).toBeNull();
  });

  it("proves(a-run-page-hash-is-a-named-proof-anchor): a click copies the hash and reveals the proof", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.assign(globalThis.navigator, { clipboard: { writeText } });
    render(ProofAnchor, {
      props: {
        label: runPageCopy.workflowRevision,
        seals: runPageCopy.sealsWorkflow,
        value
      }
    });

    expect(screen.queryByText(value)).toBeNull();
    await fireEvent.click(screen.getByRole("button", { name: runPageCopy.workflowRevision }));

    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText).toHaveBeenCalledWith(value);
    expect(screen.getByText(value).isConnected).toBe(true);
    expect(screen.getByText(runPageCopy.sealsWorkflow, { exact: false }).isConnected).toBe(true);
    await waitFor(() => expect(screen.getByText("Copied").isConnected).toBe(true));
    expect(screen.queryByRole("button", { name: "Copy" })).toBeNull();
  });

  it("proves(a-run-page-hash-is-a-named-proof-anchor): the keyboard reaches the same control", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.assign(globalThis.navigator, { clipboard: { writeText } });
    render(ProofAnchor, {
      props: {
        label: runPageCopy.terminalHash,
        seals: runPageCopy.sealsTerminal,
        value
      }
    });

    const trigger = screen.getByRole("button", { name: runPageCopy.terminalHash });
    expect(trigger.tagName).toBe("BUTTON");
    trigger.focus();
    expect(document.activeElement).toBe(trigger);
    await fireEvent.click(trigger);
    expect(writeText).toHaveBeenCalledWith(value);
  });
});
