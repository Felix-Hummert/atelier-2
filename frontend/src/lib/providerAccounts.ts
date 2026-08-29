import type { AuthProfileRevision } from "../api/client";
import { providerAccount, settingsPageCopy } from "./settingsPageCopy";

export interface ProviderAccountRowView {
  providerId: string;
  profileId: string;
  revisionNumber: number;
  authProfileRevisionHash: string;
  caption: string;
  secretPresentLabel: string;
}

export function presentProviderAccounts(
  profiles: readonly AuthProfileRevision[]
): ProviderAccountRowView[] {
  const latest = new Map<string, AuthProfileRevision>();
  for (const profile of profiles) {
    const key = `${profile.provider_id}\0${profile.profile_id}`;
    const held = latest.get(key);
    if (held === undefined || profile.revision_number > held.revision_number) {
      latest.set(key, profile);
    }
  }
  return [...latest.values()]
    .sort((left, right) => {
      if (left.provider_id !== right.provider_id) {
        return left.provider_id < right.provider_id ? -1 : 1;
      }
      if (left.profile_id !== right.profile_id) {
        return left.profile_id < right.profile_id ? -1 : 1;
      }
      return 0;
    })
    .map((profile) => ({
      providerId: profile.provider_id,
      profileId: profile.profile_id,
      revisionNumber: profile.revision_number,
      authProfileRevisionHash: profile.auth_profile_revision_hash,
      caption: providerAccount(profile.provider_id, profile.profile_id),
      secretPresentLabel: settingsPageCopy.neverShownAgain
    }));
}
