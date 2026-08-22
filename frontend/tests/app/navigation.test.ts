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

    await fireEvent.click(screen.getByRole("link", { name: "Start a run" }));
    expect((await screen.findByRole("heading", { name: "Choose a workflow" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier/new");

    window.history.back();

    await waitFor(() => expect(window.location.pathname).toBe("/atelier/project"));
    expect((await screen.findByRole("heading", { name: THE_ONE_PROJECT })).isConnected).toBe(true);
    expect(screen.queryByRole("heading", { name: "Choose a workflow" })).toBeNull();
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

  it("walks Board and History on the rail and leaves deferred destinations unclickable", async () => {
    openAt("/atelier");
    await screen.findByRole("heading", { name: "Board" });

    const rail = screen.getByRole("navigation", { name: "Workshop" });
    expect(within(rail).getByRole("link", { name: "Board" }).getAttribute("aria-current")).toBe(
      "page"
    );
    expect(within(rail).getByRole("link", { name: "History" }).getAttribute("href")).toBe(
      "/atelier/project"
    );
    expect(within(rail).queryByRole("link", { name: "Chat" })).toBeNull();
    expect(within(rail).queryByRole("link", { name: "Workflows" })).toBeNull();
    expect(within(rail).getByText("Chat").closest("[aria-disabled='true']")?.getAttribute("title")).toContain(
      "#7"
    );
    expect(within(rail).getByText("Workflows").closest("[aria-disabled='true']")?.getAttribute("title")).toContain(
      "REQ-UI-05"
    );

    await fireEvent.click(within(rail).getByRole("link", { name: "History" }));
    expect((await screen.findByRole("heading", { name: THE_ONE_PROJECT })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier/project");

    const still = screen.getByRole("navigation", { name: "Workshop" });
    await fireEvent.click(within(still).getByText("Workflows"));
    expect(window.location.pathname).toBe("/atelier/project");

    await fireEvent.click(within(still).getByRole("link", { name: "Board" }));
    expect((await screen.findByRole("heading", { name: "Board" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier");
  });

  it("names the new-run trail with the same project the other levels use", async () => {
    openAt("/atelier/new");
    await screen.findByRole("heading", { name: "Choose a workflow" });

    const trail = screen.getByRole("navigation", { name: "Where you are" });
    expect(within(trail).getAllByRole("link").map((step) => step.textContent?.trim())).toEqual([
      "Board",
      THE_ONE_PROJECT
    ]);
    expect(within(trail).getByText("New run").isConnected).toBe(true);
    expect(screen.queryByRole("link", { name: "← Project" })).toBeNull();
  });
});
