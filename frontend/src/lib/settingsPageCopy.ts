/** Short operator-facing labels for the Settings surface. */
export const settingsPageCopy = {
  label: "settings",
  unavailable: "Settings unavailable",
  sourcesTitle: "Sources",
  sourcesLabel: "sources",
  sourcesUnavailable: "Sources unavailable",
  sourceKind: "Kind",
  source: "Source",
  sourceAddress: "Address",
  sourceAuthMethod: "Authentication",
  sourceRevision: "Revision",
  issues: "issues",
  modelsTitle: "Models",
  modelsLabel: "models",
  modelsUnavailable: "Models unavailable",
  modelsEmpty: "No models available",
  model: "Model",
  provider: "Provider",
  executorRevision: "Executor revision",
  discovery: "Models are listed as published; discovery from the connected providers follows.",
  defaultsTitle: "Model defaults",
  defaultsNoCheckedModels: "Check a model above before choosing defaults",
  defaultsUnavailableModels: "No startable models are available",
  writeFailed: "Change not saved",
  registryChanged: "The model list changed. Retry to save against the current list.",
  account: "Account",
  registry: "Registry",
  unknownAccount: "Unknown account",
  addModel: "Add a model",
  checking: "Checking",
  running: "Working",
  correctTheId: "Correct the id",
  alreadyPresent: "already present",
  oneSourceToday: "This project holds one source today, several are not built yet.",
  add: "Add",
  cancel: "Cancel",
  check: "Check",
  remove: "Remove",
  notCheckedYet: "◇ not checked yet",
  difficulty: "Difficulty",
  changeSavedDefault: "Change saved default",
  noDefault: "No default",
  retry: "Retry",
  unknownAtProvider: "◇ unknown at provider",
  addedByYouChecked: "added by you · ✓ checked",
  addedByYouNotChecked: "added by you · ◇ not checked yet",
  unavailableSavedModel: "Unavailable saved model",
  connectASource: "Connect a source",
  connect: "Connect",
  disconnect: "Disconnect",
  keepIt: "Keep it",
  renewToken: "Renew token",
  where: "Where",
  token: "Token",
  items: "Items",
  library: "Library",
  neverShownAgain: "Never shown again.",
  tokenRefused: "This token was refused.",
  sourceInvalid: "This source could not be connected.",
  sourceConnectRefused: "This connection was refused.",
  sourceDisconnectRefused: "This source could not be disconnected.",
  sourceNotShown: "This source could not be shown.",
  connectionTimeNotRecorded: "connection time is not recorded",
  connected: "connected",
  thisConnection: "this connection",
  goes: "Goes",
  stays: "Stays",
  again: "Again",
  connectStartsNew: "Connect a source starts a new connection",
  theModels: "the models",
  github: "GitHub",
  gitlab: "GitLab",
  and: "and"
} as const;

export function accountChoice(modelId: string, account: string): string {
  return `${modelId} · Account ${account}`;
}

export function retainedAccountChoice(modelId: string, account: string): string {
  return `${accountChoice(modelId, account)} — Unavailable`;
}

export function difficultyLabel(difficulty: number): string {
  return `${settingsPageCopy.difficulty} ${difficulty}`;
}

export function noSuchModel(providerId: string): string {
  return `No such model at ${providerId}.`;
}

export function providerAccount(providerId: string, profileId: string): string {
  return `${providerId} · ${profileId}`;
}

export function disconnectTitle(address: string): string {
  return `Disconnect ${address}?`;
}

export function sourceAlreadyPresent(address: string): string {
  return `${address} ${settingsPageCopy.alreadyPresent}`;
}
