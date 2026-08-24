import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import App from "../../src/App.svelte";
import { MutationJournal } from "../../src/lib/mutationJournal";
import { THE_ONE_PROJECT } from "../../src/lib/project";
import { cockpitApiStub } from "../support/cockpitApi";

beforeEach(() => {
  sessionStorage.clear();
});

afterEach(() => cleanup());

function openAt(pathname: string) {
  window.history.replaceState(null, "", pathname);
  return render(App, {
    props: {
      cockpitApi: cockpitApiStub(),
      mutationJournal: new MutationJournal(sessionStorage)
    }
  });
}

describe("cockpit navigation", () => {
  it("answers a path that is no route with a not-found page that leads home to the Board", async () => {
    openAt("/atelier/nowhere");

    expect(screen.getByRole("heading", { name: "Page not found" }).isConnected).toBe(true);

    const missing = screen.getByRole("heading", { name: "Page not found" }).closest("section");
    expect(missing).not.toBeNull();
    await fireEvent.click(within(missing as HTMLElement).getByRole("link", { name: "Board" }));

    expect((await screen.findByRole("heading", { name: "Board" })).isConnected).toBe(true);
    expect(screen.queryByRole("heading", { name: "Page not found" })).toBeNull();
  });

  it("answers the bare /atelier entry with the Board itself, rewriting no path and pushing no history", async () => {
    const historyLength = window.history.length;

    openAt("/atelier");

    expect((await screen.findByRole("heading", { name: "Board" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier");
    expect(window.history.length).toBe(historyLength);
  });

  it("returns to the previously shown page when the operator presses Back", async () => {
    openAt("/atelier/project");
    await screen.findByRole("heading", { name: THE_ONE_PROJECT });

    await fireEvent.click(screen.getByRole("link", { name: "Workflows" }));
    expect((await screen.findByRole("heading", { name: "Workflows" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier/workflows");

    window.history.back();

    await waitFor(() => expect(window.location.pathname).toBe("/atelier/project"));
    expect((await screen.findByRole("heading", { name: THE_ONE_PROJECT })).isConnected).toBe(true);
    expect(screen.queryByRole("heading", { name: "Workflows" })).toBeNull();
  });

  it("names the rail's project slot honestly — shown, not a live switcher", async () => {
    openAt("/atelier");
    await screen.findByRole("heading", { name: "Board" });

    const rail = screen.getByRole("navigation", { name: "Workshop" });
    expect(within(rail).getByText(THE_ONE_PROJECT).isConnected).toBe(true);
    expect(within(rail).getByText("switch project").isConnected).toBe(true);
    expect(within(rail).queryByRole("link", { name: new RegExp(THE_ONE_PROJECT) })).toBeNull();
    expect(within(rail).queryByRole("button", { name: new RegExp(THE_ONE_PROJECT) })).toBeNull();
  });

  it("walks all four rail destinations, none of them a dead click", async () => {
    openAt("/atelier");
    await screen.findByRole("heading", { name: "Board" });

    const rail = screen.getByRole("navigation", { name: "Workshop" });
    expect(within(rail).getByRole("link", { name: "Board" }).getAttribute("aria-current")).toBe(
      "page"
    );
    expect(within(rail).getByRole("link", { name: "Workbench" }).getAttribute("href")).toBe(
      "/atelier/chat"
    );
    expect(within(rail).getByRole("link", { name: "Workflows" }).getAttribute("href")).toBe(
      "/atelier/workflows"
    );
    expect(within(rail).getByRole("link", { name: "History" }).getAttribute("href")).toBe(
      "/atelier/history"
    );

    await fireEvent.click(within(rail).getByRole("link", { name: "Workbench" }));
    expect((await screen.findByRole("heading", { name: "Workbench" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier/chat");

    await fireEvent.click(
      within(screen.getByRole("navigation", { name: "Workshop" })).getByRole("link", {
        name: "Workflows"
      })
    );
    expect((await screen.findByRole("heading", { name: "Workflows" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier/workflows");

    const still = screen.getByRole("navigation", { name: "Workshop" });
    await fireEvent.click(within(still).getByRole("link", { name: "History" }));
    expect((await screen.findByRole("heading", { name: "History" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier/history");

    await fireEvent.click(within(still).getByRole("link", { name: "Board" }));
    expect((await screen.findByRole("heading", { name: "Board" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier");
  });

  it("marks the rail destination a page belongs to, so the operator is never lost", async () => {
    openAt("/atelier/new");
    await screen.findByRole("heading", { name: "Choose a workflow" });

    const rail = screen.getByRole("navigation", { name: "Workshop" });

    // Starting a run is a Workflows-owned act, not a History one.
    expect(within(rail).getByRole("link", { name: "Workflows" }).getAttribute("aria-current")).toBe(
      "page"
    );
    expect(within(rail).getByRole("link", { name: "History" }).getAttribute("aria-current")).toBeNull();
  });
});
