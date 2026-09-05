import { createServer, type Server } from "node:net";
import { afterEach, describe, expect, it } from "vitest";

import { chooseHarnessPort } from "./harness-port";

const openServers: Server[] = [];

afterEach(async () => {
  delete process.env.ATELIER2_E2E_PORT;
  await Promise.all(
    openServers.splice(0).map((server) => new Promise<void>((resolve) => server.close(() => resolve())))
  );
});

describe("chooseHarnessPort", () => {
  it("memoizes its allocated port for a later evaluation in the same run", async () => {
    const firstPort = await chooseHarnessPort();

    const secondPort = await chooseHarnessPort();

    expect(secondPort).toBe(firstPort);
  });

  it("does not repeat a port that an independent run still holds", async () => {
    const firstPort = await chooseHarnessPort();
    const heldServer = createServer();
    openServers.push(heldServer);
    await new Promise<void>((resolve, reject) => {
      heldServer.once("error", reject);
      heldServer.listen(firstPort, "127.0.0.1", resolve);
    });
    delete process.env.ATELIER2_E2E_PORT;

    const secondPort = await chooseHarnessPort();

    expect(secondPort).not.toBe(firstPort);
  });

  it("honours an explicit ATELIER2_E2E_PORT instead of allocating one", async () => {
    process.env.ATELIER2_E2E_PORT = "18423";

    await expect(chooseHarnessPort()).resolves.toBe(18423);
  });

  it.each(["not-a-port", "0", "65536", "8080x", "12.9"])(
    "rejects an ATELIER2_E2E_PORT of %j",
    async (invalidValue) => {
      process.env.ATELIER2_E2E_PORT = invalidValue;

      await expect(chooseHarnessPort()).rejects.toThrow(
        `ATELIER2_E2E_PORT must be a TCP port number, got "${invalidValue}"`
      );
    }
  );
});
