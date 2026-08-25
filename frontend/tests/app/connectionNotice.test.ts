import { cleanup, render, screen, waitFor } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import ConnectionNotice from "../../src/components/ConnectionNotice.svelte";
import {
  reportConnectionLost,
  reportConnectionRestored,
  restartNoticeCopy
} from "../../src/lib/connectionState";

beforeEach(() => {
  reportConnectionRestored();
});

afterEach(() => {
  reportConnectionRestored();
  cleanup();
});

describe("the workshop's one restart notice (#700)", () => {
  it("says nothing while the connection is healthy -- stillness is the reward for what needs nothing", () => {
    render(ConnectionNotice);

    expect(screen.queryByText(restartNoticeCopy)).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("shows the one calm restart line the instant the connection is lost", async () => {
    render(ConnectionNotice);

    reportConnectionLost();

    expect((await screen.findByRole("status")).textContent).toContain(restartNoticeCopy);
  });

  it("clears the line the instant the connection is restored, with no reload", async () => {
    render(ConnectionNotice);
    reportConnectionLost();
    await screen.findByRole("status");

    reportConnectionRestored();

    await waitFor(() => {
      expect(screen.queryByRole("status")).toBeNull();
      expect(screen.queryByText(restartNoticeCopy)).toBeNull();
    });
  });
});
