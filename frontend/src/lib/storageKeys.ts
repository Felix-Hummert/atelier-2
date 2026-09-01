/**
 * Keeping browser storage-key names in one file lets the secret gate allowlist
 * one path instead of accumulating per-literal exceptions.
 */
export const MUTATION_JOURNAL_STORAGE_KEY = "atelier2.mutation-journal.v1";
export const CONDUCTOR_CONVERSATION_RUN_STORAGE_KEY = "atelier2.conductor-conversation-run";
