import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/svelte";
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
  it("answers a path that is no route with a not-found page that leads back to Runs", async () => {
    openAt("/atelier/nowhere");

    expect(screen.getByRole("heading", { name: "Page not found" }).isConnected).toBe(true);

    await fireEvent.click(screen.getByRole("link", { name: "Runs" }));

    expect((await screen.findByRole("heading", { name: "Runs" })).isConnected).toBe(true);
    expect(screen.queryByRole("heading", { name: "Page not found" })).toBeNull();
  });

  it("answers the bare /atelier entry with the Studio itself, rewriting no path and pushing no history", async () => {
    const historyLength = window.history.length;

    openAt("/atelier");

    expect((await screen.findByRole("heading", { name: "Studio" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier");
    expect(window.history.length).toBe(historyLength);
  });

  it("leads from the runs of this installation back up to the Studio", async () => {
    openAt("/atelier/runs");
    await screen.findByRole("heading", { name: "Runs" });

    await fireEvent.click(screen.getByRole("link", { name: "Studio" }));

    expect((await screen.findByRole("heading", { name: "Studio" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier");
  });

  it("returns to the previously shown page when the operator presses Back", async () => {
    openAt("/atelier/runs");
    await screen.findByRole("heading", { name: "Runs" });

    await fireEvent.click(screen.getByRole("link", { name: "New" }));
    expect((await screen.findByRole("heading", { name: "Choose a workflow" })).isConnected).toBe(true);
    expect(window.location.pathname).toBe("/atelier/new");

    window.history.back();

    await waitFor(() => expect(window.location.pathname).toBe("/atelier/runs"));
    expect((await screen.findByRole("heading", { name: "Runs" })).isConnected).toBe(true);
    expect(screen.queryByRole("heading", { name: "Choose a workflow" })).toBeNull();
  });
});
