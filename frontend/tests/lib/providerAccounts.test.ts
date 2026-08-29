import { describe, expect, it } from "vitest";

import type { AuthProfileRevision } from "../../src/api/client";
import { presentProviderAccounts } from "../../src/lib/providerAccounts";
import { providerAccount, settingsPageCopy } from "../../src/lib/settingsPageCopy";

const HASH_A = "a".repeat(64);
const HASH_B = "b".repeat(64);
const HASH_C = "c".repeat(64);

function profile(overrides: Partial<AuthProfileRevision> = {}): AuthProfileRevision {
  return {
    profile_id: "Max account",
    revision_number: 1,
    provider_id: "anthropic",
    auth_mode: "subscription",
    auth_profile_revision_hash: HASH_A,
    ...overrides
  };
}

describe("presentProviderAccounts", () => {
  it("leaves an empty listing as no rows and no empty caption", () => {
    expect(presentProviderAccounts([])).toEqual([]);
  });

  it("shows each account as its own row with a secret-present line", () => {
    const rows = presentProviderAccounts([
      profile(),
      profile({
        profile_id: "grok-felix",
        provider_id: "xai",
        auth_profile_revision_hash: HASH_B
      })
    ]);

    expect(rows.map((row) => row.caption)).toEqual([
      providerAccount("anthropic", "Max account"),
      providerAccount("xai", "grok-felix")
    ]);
    expect(rows.map((row) => row.secretPresentLabel)).toEqual([
      settingsPageCopy.neverShownAgain,
      settingsPageCopy.neverShownAgain
    ]);
  });

  it("keeps two revisions of the same account as one row", () => {
    const rows = presentProviderAccounts([
      profile({ revision_number: 1, auth_profile_revision_hash: HASH_A }),
      profile({ revision_number: 3, auth_profile_revision_hash: HASH_C })
    ]);

    expect(rows).toHaveLength(1);
    expect(rows[0]?.revisionNumber).toBe(3);
    expect(rows[0]?.authProfileRevisionHash).toBe(HASH_C);
    expect(rows[0]?.caption).toBe(providerAccount("anthropic", "Max account"));
  });

  it("never carries a secret, token, or credential value", () => {
    const row = presentProviderAccounts([profile()])[0];
    if (row === undefined) throw new Error("expected one account row");

    expect(Object.keys(row).sort()).toEqual([
      "authProfileRevisionHash",
      "caption",
      "profileId",
      "providerId",
      "revisionNumber",
      "secretPresentLabel"
    ]);
    const { secretPresentLabel, ...rest } = row;
    expect(JSON.stringify(rest)).not.toMatch(/token|secret|credential|password|api_key/i);
    expect(secretPresentLabel).toBe(settingsPageCopy.neverShownAgain);
  });
});
