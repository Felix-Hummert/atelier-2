/** Short operator-facing labels for the Settings surface. */
export const settingsPageCopy = {
  label: "settings",
  unavailable: "Settings unavailable",
  sourcesTitle: "Sources",
  sourcesLabel: "sources",
  sourcesUnavailable: "Sources unavailable",
  sourcesEmpty: "No source connected",
  sourceKind: "Kind",
  sourceAddress: "Address",
  sourceAuthMethod: "Authentication",
  sourceRevision: "Revision",
  modelsTitle: "Models",
  modelsLabel: "models",
  modelsUnavailable: "Models unavailable",
  modelsEmpty: "No models available",
  model: "Model",
  provider: "Provider",
  executorRevision: "Executor revision",
  discovery: "Models are listed as published; discovery from the connected providers follows.",
  defaultsTitle: "Model defaults",
  defaultsEmptyRegistry: "No models are registered",
  defaultsNoCheckedModels: "Check a model above before choosing defaults",
  defaultsUnavailableModels: "No startable models are available",
  writeFailed: "Change not saved",
  account: "Account",
  registry: "Registry",
  unknownAccount: "Unknown account",
  addModel: "Add a model",
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
  unavailableSavedModel: "Unavailable saved model"
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
