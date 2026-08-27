import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import ProofAnchor from "../../src/components/ProofAnchor.svelte";
import { shortFingerprint } from "../../src/lib/fingerprint";
import { copyLabel, proofAnchorCopy } from "../../src/lib/proofAnchorCopy";
import { runPageCopy } from "../../src/lib/runPageCopy";

const value = `${"a".repeat(60)}9594`;

afterEach(() => cleanup());

describe("the named proof affordance", () => {
  it("proves(a-run-page-hash-is-a-named-proof-anchor): the name, the value and what it seals stand together", () => {
    render(ProofAnchor, {
      props: { label: runPageCopy.promptHash, seals: runPageCopy.sealsPrompt, value }
    });

    const anchor = screen.getByRole("group", { name: runPageCopy.promptHash });
    expect(within(anchor).getByText(runPageCopy.promptHash).isConnected).toBe(true);
    expect(within(anchor).getByText(shortFingerprint(value)).isConnected).toBe(true);
    expect(anchor.textContent).toContain(runPageCopy.sealsPrompt);
  });

  it("proves(a-run-page-hash-is-a-named-proof-anchor): shows its value with no click at all, so the name is never a riddle", () => {
    render(ProofAnchor, {
      props: { label: runPageCopy.terminalHash, seals: runPageCopy.sealsTerminal, value }
    });

    expect(screen.getByText(shortFingerprint(value)).isConnected).toBe(true);
  });

  it("proves(a-run-page-hash-is-a-named-proof-anchor): a click copies the whole value, not the shortened reading", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.assign(globalThis.navigator, { clipboard: { writeText } });
    render(ProofAnchor, {
      props: { label: runPageCopy.workflowRevision, seals: runPageCopy.sealsWorkflow, value }
    });

    await fireEvent.click(
      screen.getByRole("button", { name: copyLabel(runPageCopy.workflowRevision) })
    );

    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText).toHaveBeenCalledWith(value);
    await waitFor(() => expect(screen.getByText(proofAnchorCopy.copied).isConnected).toBe(true));
  });

  it("proves(a-run-page-hash-is-a-named-proof-anchor): the keyboard reaches the same control", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.assign(globalThis.navigator, { clipboard: { writeText } });
    render(ProofAnchor, {
      props: { label: runPageCopy.terminalHash, seals: runPageCopy.sealsTerminal, value }
    });

    const trigger = screen.getByRole("button", { name: copyLabel(runPageCopy.terminalHash) });
    expect(trigger.tagName).toBe("BUTTON");
    trigger.focus();
    expect(document.activeElement).toBe(trigger);
    await fireEvent.click(trigger);
    expect(writeText).toHaveBeenCalledWith(value);
  });
});
