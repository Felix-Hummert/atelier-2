import { cleanup, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "../../src/App.svelte";
import type { CockpitApi } from "../../src/api/client";
import { reportConnectionLost, reportConnectionRestored } from "../../src/lib/connectionState";
import { exactLocal } from "../../src/lib/when";
import { railCopy } from "../../src/lib/railCopy";
import { reloadPage } from "../../src/lib/pageReload";
import { resetVersionState } from "../../src/lib/versionState";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { cockpitApiStub, healthResource } from "../support/cockpitApi";

vi.mock("../../src/lib/pageReload", () => ({ reloadPage: vi.fn() }));

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
  reportConnectionRestored();
  resetVersionState();
  window.history.replaceState(null, "", "/atelier");
});

function openWorkshop(health: CockpitApi["health"]) {
  window.history.replaceState(null, "", "/atelier");
  return render(App, {
    props: {
      cockpitApi: cockpitApiStub({ health }),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
}

describe("the workshop shell's footer names the running serve (#1100)", () => {
  it("shows the short commit, its full hash on request, and the deploy time it loaded with", async () => {
    const commit = "1234567890abcdef1234567890abcdef12345678";
    const health = vi.fn().mockResolvedValue(
      healthResource({ source_commit: commit, serve_started_at: "2026-08-31T08:00:00Z" })
    );
    openWorkshop(health);

    const footer = await screen.findByText(/12345678/, { exact: false });

    expect(footer.textContent).toContain(exactLocal("2026-08-31T08:00:00Z"));
    expect(footer.title).toBe(commit);
  });

  it("says nothing about the serve until the loaded version is known", () => {
    const { container } = openWorkshop(vi.fn(() => new Promise<never>(() => {})));

    expect(container.querySelector(".serve-footer")).toBeNull();
  });

  it("records the baseline once recovery's health succeeds after the mount read failed", async () => {
    const commit = "d".repeat(40);
    const health = vi
      .fn()
      .mockRejectedValueOnce(new Error("restarting"))
      .mockResolvedValueOnce(healthResource({ source_commit: commit }));
    openWorkshop(health);

    reportConnectionLost();
    reportConnectionRestored();

    expect(await screen.findByTitle(commit)).not.toBeNull();
    expect(screen.queryByText(railCopy.newVersionAvailable)).toBeNull();
  });

  it("announces a new version once a reconnect's health answers a different commit, without reloading on its own", async () => {
    const health = vi
      .fn()
      .mockResolvedValueOnce(healthResource({ source_commit: "a".repeat(40) }))
      .mockResolvedValueOnce(healthResource({ source_commit: "b".repeat(40) }));
    openWorkshop(health);
    await screen.findByTitle("a".repeat(40));

    reportConnectionLost();
    reportConnectionRestored();

    const notice = await screen.findByText(railCopy.newVersionAvailable);
    expect(notice.closest(".serve-footer")?.textContent).toContain(railCopy.newVersionAvailable);
    expect(reloadPage).not.toHaveBeenCalled();
    expect(screen.queryByTitle("a".repeat(40))).toBeNull();

    screen.getByRole("button", { name: railCopy.reload }).click();
    expect(reloadPage).toHaveBeenCalledOnce();
  });

  it("keeps naming the loaded commit across a reconnect that answers the same commit", async () => {
    const sameCommit = "c".repeat(40);
    const health = vi.fn().mockResolvedValue(healthResource({ source_commit: sameCommit }));
    openWorkshop(health);
    await screen.findByTitle(sameCommit);

    reportConnectionLost();
    reportConnectionRestored();

    expect(await screen.findByTitle(sameCommit)).not.toBeNull();
    expect(screen.queryByText(railCopy.newVersionAvailable)).toBeNull();
  });
});
