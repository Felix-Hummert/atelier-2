import type { AgentConfigurationRevision } from "../api/client";

/**
 * Last named-agent pick per role, operator-local.
 *
 * This is not the project-configuration owner for a recommended occupancy.
 * That owner is a later head. localStorage only remembers what this browser
 * chose, so a daily start can skip the form.
 */
export const NAMED_AGENT_CHOICE_STORAGE_KEY = "atelier.named-agent-choice";

export function authModeLabel(mode: AgentConfigurationRevision["auth_mode"]): string {
  return mode === "api_key" ? "API key" : "Subscription";
}

export function namedAgentLabel(item: AgentConfigurationRevision): string {
  return `${item.provider_id} · ${item.model} · ${authModeLabel(item.auth_mode)}`;
}

export function readLastNamedAgentChoices(
  storage: Pick<Storage, "getItem">
): Record<string, string> {
  const raw = storage.getItem(NAMED_AGENT_CHOICE_STORAGE_KEY);
  if (raw === null) return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {};
    }
    const choices: Record<string, string> = {};
    for (const [role, hash] of Object.entries(parsed)) {
      if (typeof hash === "string" && hash.length > 0) choices[role] = hash;
    }
    return choices;
  } catch {
    return {};
  }
}

export function rememberNamedAgentChoice(
  storage: Pick<Storage, "getItem" | "setItem">,
  role: string,
  hash: string
): void {
  const choices = readLastNamedAgentChoices(storage);
  choices[role] = hash;
  storage.setItem(NAMED_AGENT_CHOICE_STORAGE_KEY, JSON.stringify(choices));
}
