import type {
  AgentConfigurationInput,
  AgentConfigurationRevisionListItem,
  AuthProfileRevision,
  ExactModelRegistryRevisionWrite,
  ModelRegistryRevision
} from "../api/client";

export const MODEL_ID = /^\S+$/;

export function trimmedModelId(raw: string): string {
  return raw.trim();
}

export function offeredAccounts(
  profiles: AuthProfileRevision[],
  configurations: AgentConfigurationRevisionListItem[]
): AuthProfileRevision[] {
  return profiles
    .filter((profile) =>
      configurations.some((item) => item.provider_id === profile.provider_id)
    )
    .sort((left, right) => {
      if (left.provider_id !== right.provider_id) {
        return left.provider_id < right.provider_id ? -1 : 1;
      }
      if (left.profile_id !== right.profile_id) {
        return left.profile_id < right.profile_id ? -1 : 1;
      }
      return 0;
    });
}

export function pickExecutorPin(
  providerId: string,
  authProfileRevisionHash: string,
  configurations: AgentConfigurationRevisionListItem[]
): AgentConfigurationRevisionListItem | null {
  const sameProvider = configurations.filter((item) => item.provider_id === providerId);
  if (sameProvider.length === 0) return null;
  const matchingProfile = sameProvider.filter(
    (item) => item.auth_profile_revision_hash === authProfileRevisionHash
  );
  const pool = matchingProfile.length > 0 ? matchingProfile : sameProvider;
  return pool.reduce((best, item) =>
    item.agent_configuration_revision_hash < best.agent_configuration_revision_hash
      ? item
      : best
  );
}

export type AddModelPlan =
  | { kind: "invalid-id" }
  | { kind: "no-pin" }
  | {
      kind: "duplicate";
      entry: ModelRegistryRevision["entries"][number];
      registry: ModelRegistryRevision;
    }
  | {
      kind: "create";
      input: AgentConfigurationInput;
      pin: AgentConfigurationRevisionListItem;
      registryWrite: (newHash: string) => ExactModelRegistryRevisionWrite;
    };

export function planAddModel(args: {
  modelId: string;
  profile: AuthProfileRevision;
  configurations: AgentConfigurationRevisionListItem[];
  registries: ModelRegistryRevision[];
}): AddModelPlan {
  const modelId = trimmedModelId(args.modelId);
  if (!MODEL_ID.test(modelId)) {
    return { kind: "invalid-id" };
  }
  for (const registry of args.registries) {
    if (registry.provider_id !== args.profile.provider_id) continue;
    const entry = registry.entries.find((candidate) => candidate.model_id === modelId);
    if (entry !== undefined) {
      return { kind: "duplicate", entry, registry };
    }
  }
  const pin = pickExecutorPin(
    args.profile.provider_id,
    args.profile.auth_profile_revision_hash,
    args.configurations
  );
  if (pin === null) {
    return { kind: "no-pin" };
  }
  const registry = args.registries.find(
    (candidate) => candidate.provider_id === args.profile.provider_id
  );
  const input: AgentConfigurationInput = {
    model: modelId,
    auth_profile_revision_hash: args.profile.auth_profile_revision_hash,
    executor_revision: pin.executor_revision,
    requested_capability: pin.requested_capability
  };
  return {
    kind: "create",
    input,
    pin,
    registryWrite: (newHash) => {
      const entries = [
        ...(registry?.entries ?? []).map((entry) => ({
          model_id: entry.model_id,
          agent_configuration_revision_hash: entry.agent_configuration_revision_hash
        })),
        {
          model_id: modelId,
          agent_configuration_revision_hash: newHash
        }
      ];
      const writeInput = {
        revision_number: (registry?.revision_number ?? 0) + 1,
        entries
      };
      return { input: writeInput, body: JSON.stringify(writeInput) };
    }
  };
}

export type RowPresentation =
  | "checking"
  | "unknown"
  | "added-checked"
  | "added-not-checked"
  | "none";

export function rowPresentation(
  entry: { source: string; provider_check: string },
  checking: boolean
): RowPresentation {
  if (checking) return "checking";
  if (entry.provider_check === "unknown-at-provider") return "unknown";
  if (entry.source === "operator" && entry.provider_check === "checked") {
    return "added-checked";
  }
  if (entry.source === "operator" && entry.provider_check === "not-checked") {
    return "added-not-checked";
  }
  return "none";
}
