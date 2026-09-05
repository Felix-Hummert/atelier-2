import { createServer } from "node:net";

const LOOPBACK_ADDRESS = "127.0.0.1";
const MAX_TCP_PORT = 65535;

/**
 * The one place that decides which TCP port `serve_cockpit.py` binds to for a
 * Playwright run. An operator- or CI-supplied `ATELIER2_E2E_PORT` always
 * wins; otherwise the OS hands out a free loopback port, so two worktrees
 * running the e2e suite at the same time never collide on the harness that
 * `frontend/playwright.config.ts` starts.
 *
 * Playwright's own worker processes re-import this config and inherit the
 * launching process's environment, so an allocated port is written back into
 * `ATELIER2_E2E_PORT` once chosen: every evaluation within one Playwright run
 * then agrees on the same port, while a separate run starts with no such
 * variable and allocates its own.
 */
export async function chooseHarnessPort(): Promise<number> {
  const explicitPort = process.env.ATELIER2_E2E_PORT;
  if (explicitPort !== undefined) {
    return parseTcpPort(explicitPort);
  }
  const allocatedPort = await allocateFreeLoopbackPort();
  process.env.ATELIER2_E2E_PORT = String(allocatedPort);
  return allocatedPort;
}

const DECIMAL_STRING = /^[0-9]+$/;

function parseTcpPort(value: string): number {
  const parsed = Number.parseInt(value, 10);
  if (!DECIMAL_STRING.test(value) || parsed <= 0 || parsed > MAX_TCP_PORT) {
    throw new Error(`ATELIER2_E2E_PORT must be a TCP port number, got "${value}"`);
  }
  return parsed;
}

function allocateFreeLoopbackPort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.on("error", reject);
    probe.listen(0, LOOPBACK_ADDRESS, () => {
      const address = probe.address();
      if (address === null || typeof address === "string") {
        reject(new Error("expected a TCP AddressInfo from an ephemeral-port listener"));
        return;
      }
      const { port } = address;
      probe.close((closeError) => (closeError ? reject(closeError) : resolve(port)));
    });
  });
}
