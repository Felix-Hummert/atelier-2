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

function rail(): HTMLElement {
  return screen.getByRole("navigation", { name: "Workshop" });
}

describe("cockpit navigation", () => {
  it("answers a path that is no route with a not-found page that leads home to the Workbench", async () => {
    openAt("/atelier/nowhere");

    expect(screen.getByRole("heading", { name: "Page not found" }).isConnected).toBe(true);

    const missing = screen.getByRole("heading", { name: "Page not found" }).closest("section");
    expect(missing).not.toBeNull();
    await fireEvent.click(within(missing as HTMLElement).getByRole("link", { name: "Workbench" }));

    expect((await screen.findByRole("heading", { name: "Workbench" })).isConnected).toBe(true);
    expect(screen.queryByRole("heading", { name: "Page not found" })).toBeNull();
  });

  it("answers the bare /atelier entry with the Workbench itself, rewriting no path and pushing no history", async () => {
    const historyLength = window.history.length;

    openAt("/atelier");

    expect((await screen.findByRole("heading", { name: "Workbench" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier");
    expect(window.history.length).toBe(historyLength);
  });

  it("returns to the previously shown page when the operator presses Back", async () => {
    openAt("/atelier/project");
    await screen.findByRole("heading", { name: THE_ONE_PROJECT });

    await fireEvent.click(screen.getByRole("link", { name: "Catalog" }));
    expect((await screen.findByRole("heading", { name: "Catalog" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier/catalog");

    window.history.back();

    await waitFor(() => expect(window.location.pathname).toBe("/atelier/project"));
    expect((await screen.findByRole("heading", { name: THE_ONE_PROJECT })).isConnected).toBe(true);
    expect(screen.queryByRole("heading", { name: "Catalog" })).toBeNull();
  });

  it("carries the project name under Settings at the rail's foot, as the context over the rooms", async () => {
    openAt("/atelier");
    await screen.findByRole("heading", { name: "Workbench" });

    const settings = within(rail()).getByRole("link", { name: new RegExp("Settings") });
    expect(within(settings).getByText(THE_ONE_PROJECT).isConnected).toBe(true);
    expect(settings.getAttribute("href")).toBe("/atelier/project");
  });

  it("walks the three rooms and Settings, none of them a dead click", async () => {
    openAt("/atelier");
    await screen.findByRole("heading", { name: "Workbench" });

    expect(within(rail()).getByRole("link", { name: "Workbench" }).getAttribute("aria-current")).toBe(
      "page"
    );
    expect(within(rail()).getByRole("link", { name: "Catalog" }).getAttribute("href")).toBe(
      "/atelier/catalog"
    );
    expect(within(rail()).getByRole("link", { name: "History" }).getAttribute("href")).toBe(
      "/atelier/history"
    );
    // The rooms the picture retired are no entry at all, not a disabled one.
    expect(within(rail()).queryByRole("link", { name: "Board" })).toBeNull();
    expect(within(rail()).queryByRole("link", { name: "Workflows" })).toBeNull();

    await fireEvent.click(within(rail()).getByRole("link", { name: "Catalog" }));
    expect((await screen.findByRole("heading", { name: "Catalog" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier/catalog");

    await fireEvent.click(within(rail()).getByRole("link", { name: "History" }));
    expect((await screen.findByRole("heading", { name: "History" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier/history");

    await fireEvent.click(within(rail()).getByRole("link", { name: new RegExp("Settings") }));
    expect((await screen.findByRole("heading", { name: THE_ONE_PROJECT })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier/project");

    await fireEvent.click(within(rail()).getByRole("link", { name: "Workbench" }));
    expect((await screen.findByRole("heading", { name: "Workbench" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier/chat");
  });

  it("marks the rail entry a page belongs to, so the operator is never lost", async () => {
    openAt("/atelier/new");
    await screen.findByRole("heading", { name: "Choose a workflow" });

    // Starting a run belongs to the Catalog: it is the one room a workflow is
    // found and started from (ADR 0019 §1).
    expect(within(rail()).getByRole("link", { name: "Catalog" }).getAttribute("aria-current")).toBe(
      "page"
    );
    expect(within(rail()).getByRole("link", { name: "History" }).getAttribute("aria-current")).toBeNull();
  });
});
