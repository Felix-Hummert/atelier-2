import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import App from "../../src/App.svelte";
import { MutationJournal } from "../../src/lib/mutationJournal";
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
  it("answers a path that is no route with a not-found page that leads home to the Studio", async () => {
    openAt("/atelier/nowhere");

    expect(screen.getByRole("heading", { name: "Page not found" }).isConnected).toBe(true);

    const missing = screen.getByRole("heading", { name: "Page not found" }).closest("section");
    expect(missing).not.toBeNull();
    await fireEvent.click(within(missing as HTMLElement).getByRole("link", { name: "Studio" }));

    expect((await screen.findByRole("heading", { name: "Studio" })).isConnected).toBe(true);
    expect(screen.queryByRole("heading", { name: "Page not found" })).toBeNull();
  });

  it("answers the bare /atelier entry with the Studio itself, rewriting no path and pushing no history", async () => {
    const historyLength = window.history.length;

    openAt("/atelier");

    expect((await screen.findByRole("heading", { name: "Studio" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier");
    expect(window.history.length).toBe(historyLength);
  });

  it("returns to the previously shown page when the operator presses Back", async () => {
    openAt("/atelier/project");
    await screen.findByRole("heading", { name: "This workshop" });

    await fireEvent.click(screen.getByRole("link", { name: "Start a run" }));
    expect((await screen.findByRole("heading", { name: "Choose a workflow" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier/new");

    window.history.back();

    await waitFor(() => expect(window.location.pathname).toBe("/atelier/project"));
    expect((await screen.findByRole("heading", { name: "This workshop" })).isConnected).toBe(true);
    expect(screen.queryByRole("heading", { name: "Choose a workflow" })).toBeNull();
  });

  it("opens the one project from the topbar chip", async () => {
    openAt("/atelier");
    await screen.findByRole("heading", { name: "Studio" });

    await fireEvent.click(screen.getByRole("button", { name: /This workshop/ }));

    expect((await screen.findByRole("heading", { name: "This workshop" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier/project");
  });

  it("walks Studio and Projekte on the rail and leaves deferred destinations unclickable", async () => {
    openAt("/atelier");
    await screen.findByRole("heading", { name: "Studio" });

    const rail = screen.getByRole("navigation", { name: "Workshop" });
    expect(within(rail).getByRole("link", { name: "Studio" }).getAttribute("aria-current")).toBe(
      "page"
    );
    expect(within(rail).getByRole("link", { name: "Projekte" }).getAttribute("href")).toBe(
      "/atelier/project"
    );
    expect(within(rail).queryByRole("link", { name: "Runs" })).toBeNull();
    expect(within(rail).queryByRole("link", { name: "Library" })).toBeNull();
    expect(within(rail).queryByRole("link", { name: "Settings" })).toBeNull();
    expect(within(rail).getByText("Runs").closest("[aria-disabled='true']")?.getAttribute("title")).toContain(
      "REQ-UI-13"
    );
    expect(within(rail).getByText("Library").closest("[aria-disabled='true']")?.getAttribute("title")).toContain(
      "REQ-UI-05"
    );
    expect(within(rail).getByText("Settings").closest("[aria-disabled='true']")?.getAttribute("title")).toContain(
      "REQ-UI-15"
    );

    await fireEvent.click(within(rail).getByRole("link", { name: "Projekte" }));
    expect((await screen.findByRole("heading", { name: "This workshop" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier/project");

    const still = screen.getByRole("navigation", { name: "Workshop" });
    await fireEvent.click(within(still).getByText("Library"));
    expect(window.location.pathname).toBe("/atelier/project");

    await fireEvent.click(within(still).getByRole("link", { name: "Studio" }));
    expect((await screen.findByRole("heading", { name: "Studio" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier");
  });

  it("names the new-run trail with the same project the other levels use", async () => {
    openAt("/atelier/new");
    await screen.findByRole("heading", { name: "Choose a workflow" });

    const trail = screen.getByRole("navigation", { name: "Where you are" });
    expect(within(trail).getAllByRole("link").map((step) => step.textContent?.trim())).toEqual([
      "Studio",
      "This workshop"
    ]);
    expect(within(trail).getByText("New run").isConnected).toBe(true);
    expect(screen.queryByRole("link", { name: "← Project" })).toBeNull();
  });
});
