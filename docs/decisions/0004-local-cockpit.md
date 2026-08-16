# ADR 0004: The local cockpit is a projection and control adapter

- Status: ACCEPTED 2026-08-12 — implemented: the cockpit landed with this record
  and keeps no durable run state of its own

## Context

The durable API exposes enough truth to operate the first workflow vertical,
but a browser client can still damage that model by keeping a second run state,
assuming delivery means completion, or retrying a freshly constructed command
after an uncertain response. The first cockpit also needs a usable small-screen
surface without claiming a remote access or authentication boundary that does
not exist.

## Decision

The cockpit is a Svelte single-page application built as static files and served
by the Python host on the API's origin. It uses native browser History, Fetch,
and EventSource behind one typed client. Closed Zod decoders refuse resources or
events that do not match the API contract. The run page retains the last
confirmed snapshot while refreshing and projects only the immutable workflow
revision, current durable run resource, and contiguous durable event history.
It owns no run state machine or domain event log. A stream that ended because it
failed is never shown as one that finished: the server's terminal failure frame
stops the stream, marks the connection stopped, and prints the API's own problem
title and detail instead of a locally invented message.

Browser mutation state is limited to one session-scoped delivery journal. Before
sending publish, start, Wait-answer, or reconciliation requests, the cockpit
stores the exact target and body bytes under their stable identity. An uncertain
retry reuses those bytes. A response can clear the entry only when it proves the
same request; pending Wait and reconciliation commands remain visible as
Working. Reconciliation clears only after the exact durable resolved event. This
journal is delivery evidence, not durable workflow truth, and browser-profile
loss never changes server state.

Operator-visible state uses words, shapes, and color together. The narrow layout
keeps one primary question in the action card, meets touch sizing, preserves
keyboard focus through dialogs and state changes, and does not rely on motion.
Real-Chromium acceptance drives the production static build against the composed
local host at mobile and desktop widths, with keyboard, reduced-motion,
grayscale, overflow, and automated accessibility checks.

## Consequences

- Reloading or reconnecting recovers from the API and durable event history;
  local state cannot make a run complete or resolved.
- The session journal prevents accidental payload changes after ambiguous
  delivery, but it is intentionally neither cross-browser storage nor a server
  queue.
- The cockpit is currently safe only on the trusted local boundary. It adds no
  authentication, public deployment, provider selection, or platform adapter.
- A future remote shell may replace the host and access boundary without
  changing the durable API or the cockpit's projection rules.

## Supersedes

None.
