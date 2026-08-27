import type { AgentConfigurationRevisionListItem } from "../api/client";

function authModeLabel(mode: AgentConfigurationRevisionListItem["auth_mode"]): string {
  return mode === "api_key" ? "API key" : "Subscription";
}

export function agentConfigurationLabel(item: AgentConfigurationRevisionListItem): string {
  return `${item.provider_id} · ${item.model} · ${authModeLabel(item.auth_mode)}`;
}
