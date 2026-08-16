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
  it("answers a path that is no route with a not-found page that leads home to the Studio", async () => {
    openAt("/atelier/nowhere");

    expect(screen.getByRole("heading", { name: "Page not found" }).isConnected).toBe(true);

    await fireEvent.click(screen.getByRole("link", { name: "Studio" }));

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
});
