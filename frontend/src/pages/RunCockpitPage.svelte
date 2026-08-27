<script lang="ts">
  import { onMount, tick } from "svelte";

  import {
    CockpitRequestError,
    executableGraph,
    parseEventCursor,
    type CockpitApi,
    isRunV3,
    type AnyRun,
    type Problem,
    type Run,
    type RunV3,
    type RunEvent,
    type RunEventSubscription,
    type WorkflowRevisionDetail
  } from "../api/client";
  import BackLink from "../components/BackLink.svelte";
  import HumanActionCard from "../components/HumanActionCard.svelte";
  import NodeRail from "../components/NodeRail.svelte";
  import ProblemNotice from "../components/ProblemNotice.svelte";
  import ProofAnchor from "../components/ProofAnchor.svelte";
  import ReconciliationActionCard from "../components/ReconciliationActionCard.svelte";
  import {
    MutationJournal,
    reconciliationCommand,
    reconciliationMutation,
    waitAnswer,
    waitMutation,
    waitMutationId,
    type JournalEntry,
    type ReconciliationCommand,
    type ReconciliationDeterminationInput,
    type ReconciliationMutation,
    type WaitMutation
  } from "../lib/mutationJournal";
  import V3RunView from "../components/V3RunView.svelte";
  import { wrapDisplayCopy } from "../lib/displayCopy";
  import {
    beginRead,
    confirmRead,
    failRead,
    retainedRead,
    type RetainedRead
  } from "../lib/readResource";
  import { runPageCopy } from "../lib/runPageCopy";
  import { runHeaderCopy, runHeaderTitle } from "../lib/runPages";
  import { WORKSHOP_DESTINATION } from "../lib/workshop";
  import {
    decodeAndApplyDurableEvent,
    markComplete,
    markConnecting,
    markFailed,
    markLive,
    restartStreamProjection,
    streamProjection,
    type StreamProjection
  } from "../lib/runProjection";
  import {
    connectionLabel,
    protocolDetail,
    protocolTitle,
    streamStopped
  } from "../lib/streamStatus";

  export let cockpitApi: CockpitApi;
  export let mutationJournal: MutationJournal;
  export let publicReference: string;
  export let navigate: (path: string) => void;
  export let createReconcileCommandId: () => string;

  /**
   * The trail leads to the Workbench: living work lives there now that the
   * Board is gone. Deriving it from the run's own state -- alive to the
   * Workbench, ended to History (ADR 0019 §1) -- is a successor gap.
   */
  const cameFrom = WORKSHOP_DESTINATION.workbench;

  interface RunSnapshot {
    run: Run;
    revision: WorkflowRevisionDetail;
  }

  let snapshot: RetainedRead<RunSnapshot, Problem> = retainedRead<RunSnapshot, Problem>();
  /**
   * A version 3 run, held apart from the snapshot below.
   *
   * Everything after this point -- the revision binding, the event stream, the
   * wait and reconciliation forms, the rail -- reads fields a version 3 run does
   * not have. Branching once here keeps that whole path exactly as it was
   * instead of teaching thirty-seven readers that this format has no such thing.
   */
  let v3Run: RunV3 | null = null;
  let projection: StreamProjection | null = null;
  let stream: RunEventSubscription | null = null;
  let failureMessage: string | null = null;
  let pendingWait: Extract<JournalEntry, { kind: "wait" }> | null = null;
  let waitAccepted = false;
  let waitBusy = false;
  let waitValidationMessage: string | null = null;
  let waitFailureMessage: string | null = null;
  let humanActionCard: HumanActionCard;
  let pendingReconciliation: Extract<JournalEntry, { kind: "reconciliation" }> | null = null;
  let reconciliationAccepted = false;
  let reconciliationBusy = false;
  let reconciliationFailureMessage: string | null = null;
  let reconciliationActionCard: ReconciliationActionCard;
  let runStateElement: { focus(): void };
  let disposed = false;
  let eventQueue: Promise<void> = Promise.resolve();
  $: pendingAnswer = pendingWait === null ? null : waitAnswer(pendingWait);
  $: openFormNodeIds = new Set(
    [pendingWait?.node_id, pendingReconciliation?.node_id].filter(
      (nodeId): nodeId is string => nodeId !== undefined
    )
  );

  onMount(() => {
    void load();
    return () => {
      disposed = true;
      stream?.close();
      stream = null;
    };
  });

  async function load(): Promise<void> {
    const begun = beginRead(snapshot);
    const generation = begun.generation;
    snapshot = begun.read;
    failureMessage = null;
    try {
      const run = await cockpitApi.getRun(publicReference);
      requireRequestedRun(run);
      if (isRunV3(run)) {
        if (disposed || generation !== snapshot.generation) return;
        v3Run = run;
        snapshot = { confirmed: null, generation, request: { state: "idle" } };
        // A version 3 run has an event stream now (#249), so the page follows it
        // like any other. It opened none while the wire carried no format-3
        // event and the page said so; saying so is what became untrue.
        ensureEventStream(run);
        return;
      }
      const revision =
        snapshot.confirmed?.revision.workflow_revision_hash === run.workflow_revision_hash
          ? snapshot.confirmed.revision
          : await cockpitApi.getWorkflowRevision(run.workflow_revision_hash);
      requireBoundRevision(run, revision);
      if (disposed || generation !== snapshot.generation) return;
      snapshot = confirmRead(snapshot, generation, { run, revision });
      ensureEventStream(run);
      try {
        await loadPendingWait(run);
        await loadPendingReconciliation(run);
      } catch (error) {
        failureMessage = error instanceof Error
          ? error.message
          : runPageCopy.savedAnswerUnreadable;
      }
      if (disposed || generation !== snapshot.generation) return;
    } catch (error) {
      if (disposed || generation !== snapshot.generation) return;
      if (error instanceof CockpitRequestError && error.problem !== null) {
        snapshot = failRead(snapshot, generation, error.problem);
      } else {
        failureMessage = error instanceof Error ? error.message : runPageCopy.runUnloadable;
        snapshot = { ...snapshot, request: { state: "idle" } };
      }
    }
  }

  /** Asked of every format: the identity check runs before the run is narrowed. */
  function requireRequestedRun(run: AnyRun): void {
    if (run.public_run_reference !== publicReference) {
      throw new CockpitRequestError(runPageCopy.differentDurableRun);
    }
  }

  function requireBoundRevision(run: Run, revision: WorkflowRevisionDetail): void {
    const currentNode = executableGraph(revision.graph).nodes.find(
      (node) => node.node_id === run.current_node.node_id
    );
    if (
      revision.workflow_revision_hash !== run.workflow_revision_hash ||
      currentNode === undefined ||
      JSON.stringify(currentNode) !== JSON.stringify(run.current_node)
    ) {
      throw new CockpitRequestError(runPageCopy.workflowRevisionMismatch);
    }
  }

  function ensureEventStream(run: Run | RunV3): void {
    if (stream !== null || projection?.connection === "complete" || projection?.connection === "failed") return;
    projection = projection === null
      ? streamProjection(run.public_run_reference, run.workflow_revision_hash)
      : restartStreamProjection(
          projection,
          run.public_run_reference,
          run.workflow_revision_hash
        );
    try {
      stream = cockpitApi.openRunEvents(run.public_run_reference, {
        opened: () => {
          if (projection?.protocol_problem === null && projection.connection !== "failed") {
            projection = markLive(projection);
          }
        },
        event: applyEvent,
        disconnected: () => {
          if (projection !== null && projection.protocol_problem === null && projection.connection !== "complete" && projection.connection !== "failed") {
            projection = markConnecting(projection, true);
          }
        }
      });
    } catch (error) {
      failureMessage = error instanceof Error ? error.message : runPageCopy.eventStreamUnstartable;
      if (projection !== null) projection = markConnecting(projection, true);
    }
  }

  /**
   * Reading the run again, from scratch, whenever the page is not following it.
   *
   * This used to fire only on a protocol violation, on the theory that the
   * browser's own EventSource heals an ordinary drop. It does not always: after
   * a server restart the page sat on "Reconnecting" for eighteen minutes with
   * no way out (operator, 23.08.). So the existing subscription is dropped and
   * both the run and its stream are opened again — an act that is honest in
   * every unhealthy state. What this cannot heal — a cursor the other side no
   * longer knows — is #529.
   */
  function retryStream(): void {
    if (projection === null) return;
    stream?.close();
    stream = null;
    projection = markConnecting(projection);
    void load();
  }

  function applyEvent(rawData: string): void {
    eventQueue = eventQueue.then(() => applyEventInOrder(rawData)).catch((error: unknown) => {
      stream?.close();
      stream = null;
      if (projection !== null) projection = markFailed(projection, null);
      failureMessage = error instanceof Error
        ? error.message
        : runPageCopy.eventUnverified;
    });
  }

  async function applyEventInOrder(rawData: string): Promise<void> {
    if (projection === null) return;
    const priorSequence = projection.last_sequence;
    const confirmed = snapshot.confirmed;
    const graph = confirmed === null ? undefined : executableGraph(confirmed.revision.graph);
    const next = await decodeAndApplyDurableEvent(projection, rawData, graph);
    projection = next;
    if (next.protocol_problem !== null || next.connection === "failed") {
      stream?.close();
      stream = null;
      return;
    }
    if (next.last_sequence === priorSequence) return;
    const latest = next.events.at(-1);
    if (v3Run !== null) {
      // A version 3 line ends on its agent sink, not on a subworkflow node, so
      // the kind below cannot say "ended" for one. What can is the run itself:
      // it is re-read once a node has finished its turn, which also carries the
      // rail and the terminal hash the page is showing.
      await refreshWatchedV3Run(next);
      return;
    }
    if (latest?.event === "SUBWORKFLOW_COMPLETED") {
      projection = markComplete(next);
      stream?.close();
      stream = null;
      void followDurableEvent(latest);
    } else if (
      latest?.event === "ACTION_RECONCILIATION_REQUIRED" ||
      latest?.event === "ACTION_RECONCILIATION_RESOLVED" ||
      latest?.event === "WAITING_INPUT" ||
      latest?.event === "WAIT_ANSWERED" ||
      latest?.event === "AGENT_FAILED" ||
      latest?.event === "AGENT_CANCELLED" ||
      latest?.event === "AGENT_INTERRUPTED"
    ) {
      void followDurableEvent(latest);
    }
  }

  async function refreshWatchedV3Run(applied: StreamProjection): Promise<void> {
    let read: AnyRun;
    try {
      read = await cockpitApi.getRun(publicReference);
    } catch {
      return;
    }
    if (disposed || !isRunV3(read)) return;
    v3Run = read;
    // The run state only says the cursor will not grow. Completeness is
    // last_sequence matching that cursor. Closing on the terminal state alone
    // drops an event still in the page (the 1-vs-2 flake).
    if (read.state !== "COMPLETED" && read.state !== "FAILED") return;
    if (read.latest_event_cursor === null) return;
    const cursor = parseEventCursor(read.latest_event_cursor);
    if (cursor === null || cursor.publicRunReference !== read.public_run_reference) {
      return;
    }
    if (applied.last_sequence !== cursor.sequence) return;
    projection = markComplete(applied);
    stream?.close();
    stream = null;
  }

  async function followDurableEvent(event: RunEvent): Promise<void> {
    let journalFailure: string | null = null;
    try {
      // Only the older formats' answer card writes a wait mutation, and only
      // their event carries the decimal `answer` that card sent. A format-3
      // answer reaches the stream through the API alone, so there is no pending
      // mutation of this page's to settle against it.
      if (event.event === "WAIT_ANSWERED" && "answer" in event) {
        const mutationId = waitMutationId(event.public_run_reference, event.node_id);
        const entry = await mutationJournal.get(mutationId);
        const resolved = entry?.kind === "wait" && await mutationJournal.resolve(mutationId, {
          type: "wait_answered",
          public_run_reference: event.public_run_reference,
          workflow_revision_hash: event.workflow_revision_hash,
          node_id: event.node_id,
          answer: event.answer,
          answer_hash: event.answer_hash
        });
        if (resolved) {
          pendingWait = null;
          waitAccepted = false;
          waitFailureMessage = null;
        }
      } else if (event.event === "ACTION_RECONCILIATION_RESOLVED") {
        const commandId = event.receipt.reconcile_command_id;
        if (commandId !== null) {
          const mutationId = `reconciliation:${event.public_run_reference}:${commandId}`;
          const entry = await mutationJournal.get(mutationId);
          const source = event.receipt.confirmation_source;
          const resolved = entry?.kind === "reconciliation" &&
            (source === "OPERATOR_FOUND" || source === "OPERATOR_AUTHORIZED_EXECUTION") &&
            await mutationJournal.resolve(mutationId, {
              type: "reconciliation_resolved",
              public_run_reference: event.public_run_reference,
              workflow_revision_hash: event.workflow_revision_hash,
              node_id: event.node_id,
              command_id: commandId,
              request_hash: event.receipt.request_hash,
              effect_id: event.receipt.effect_id,
              confirmation_source: source,
              result_base64: event.receipt.result_base64,
              result_hash: event.receipt.result_hash
            });
          if (resolved) {
            pendingReconciliation = null;
            reconciliationAccepted = false;
            reconciliationFailureMessage = null;
          }
        }
      }
    } catch (error) {
      journalFailure = error instanceof Error
        ? error.message
        : runPageCopy.eventCouldNotReconcileAnswer;
    }
    await load();
    if (event.event === "WAIT_ANSWERED") await focusRunState();
    if (event.event === "ACTION_RECONCILIATION_RESOLVED") await focusRunState();
    if (journalFailure !== null) failureMessage = journalFailure;
  }

  async function loadPendingWait(run: Run): Promise<void> {
    if (run.state !== "WAITING_INPUT" || run.waiting.type !== "WAITING_INPUT") {
      pendingWait = null;
      waitAccepted = false;
      return;
    }
    const mutationId = waitMutationId(run.public_run_reference, run.waiting.node_id);
    const entry = await mutationJournal.get(mutationId);
    if (entry !== null && entry.kind !== "wait") {
      throw new Error(runPageCopy.savedRequestWrongOperation);
    }
    if (entry === null) {
      pendingWait = null;
      waitAccepted = false;
      return;
    }
    if (
      entry.workflow_revision_hash !== run.workflow_revision_hash ||
      entry.node_id !== run.waiting.node_id ||
      entry.public_run_reference !== run.public_run_reference
    ) {
      throw new Error(runPageCopy.savedAnswerWrongNode);
    }
    if (pendingWait?.mutation_id !== entry.mutation_id) waitAccepted = false;
    pendingWait = entry;
  }

  async function loadPendingReconciliation(run: Run): Promise<void> {
    if (run.state !== "WAITING_RECONCILIATION" || run.waiting.type !== "WAITING_RECONCILIATION") {
      pendingReconciliation = null;
      reconciliationAccepted = false;
      return;
    }
    const waiting = run.waiting;
    const entries = (await mutationJournal.entries()).filter(
      (entry): entry is Extract<JournalEntry, { kind: "reconciliation" }> =>
        entry.kind === "reconciliation" &&
        entry.target === `/atelier/api/v1/runs/${run.public_run_reference}/reconciliations` &&
        entry.node_id === waiting.node_id
    );
    if (entries.length > 1) {
      throw new Error(runPageCopy.multipleReconciliationsSaved);
    }
    const entry = entries[0] ?? null;
    if (entry === null) {
      pendingReconciliation = null;
      reconciliationAccepted = false;
      return;
    }
    if (
      entry.workflow_revision_hash !== run.workflow_revision_hash ||
      entry.request_base64 !== waiting.request_base64 ||
      entry.request_hash !== waiting.request_hash
    ) {
      throw new Error(runPageCopy.savedDecisionWrongReconciliation);
    }
    const pendingCommand = waiting.pending_command;
    const command = reconciliationCommand(entry);
    if (pendingCommand !== null) {
      if (!pendingCommandMatches(pendingCommand, command)) {
        throw new Error(runPageCopy.pendingCommandDiffersFromDecision);
      }
    }
    if (pendingReconciliation?.mutation_id !== entry.mutation_id) {
      reconciliationAccepted = pendingCommand !== null;
    }
    pendingReconciliation = entry;
  }

  async function submitWait(answer: string): Promise<void> {
    waitValidationMessage = null;
    waitFailureMessage = null;
    if (!/^(?:0|-?[1-9][0-9]*)$/.test(answer)) {
      waitValidationMessage = runPageCopy.canonicalInteger;
      return;
    }
    const run = snapshot.confirmed?.run;
    if (run?.state !== "WAITING_INPUT" || run.waiting.type !== "WAITING_INPUT") return;
    waitBusy = true;
    let mutation: WaitMutation | null = null;
    try {
      mutation = await waitMutation(
        run.public_run_reference,
        run.workflow_revision_hash,
        run.waiting.node_id,
        answer
      );
      const prepared = await mutationJournal.prepare(mutation);
      if (prepared.kind !== "wait") throw new Error(runPageCopy.exactRequestWrongKind);
      pendingWait = prepared;
      waitAccepted = false;
      await deliverWait(mutation);
      await focusAfterDelivery();
    } catch (error) {
      if (mutation !== null) await recordWaitFailure(mutation.mutation_id, error);
      waitFailureMessage = error instanceof Error ? error.message : runPageCopy.answerUnconfirmed;
    } finally {
      waitBusy = false;
      if (waitFailureMessage !== null) {
        await focusWaitFailure();
      }
    }
  }

  async function retryWait(): Promise<void> {
    if (pendingWait === null) return;
    waitBusy = true;
    waitFailureMessage = null;
    try {
      await deliverWait(pendingWait);
      await focusAfterDelivery();
    } catch (error) {
      await recordWaitFailure(pendingWait.mutation_id, error);
      waitFailureMessage = error instanceof Error ? error.message : runPageCopy.exactRetryUnconfirmed;
    } finally {
      waitBusy = false;
      if (waitFailureMessage !== null) {
        await focusWaitFailure();
      }
    }
  }

  async function discardWait(): Promise<void> {
    if (pendingWait === null) return;
    await mutationJournal.discard(pendingWait.mutation_id);
    pendingWait = null;
    waitAccepted = false;
    waitValidationMessage = null;
    waitFailureMessage = null;
    await tick();
    humanActionCard?.focusInput();
  }

  async function submitReconciliation(
    actor: string,
    evidence: string,
    determination: ReconciliationDeterminationInput
  ): Promise<void> {
    const run = snapshot.confirmed?.run;
    if (run?.state !== "WAITING_RECONCILIATION" || run.waiting.type !== "WAITING_RECONCILIATION") {
      return;
    }
    reconciliationBusy = true;
    reconciliationFailureMessage = null;
    let mutation: ReconciliationMutation | null = null;
    try {
      await tick();
      reconciliationActionCard?.focusStatus();
      mutation = await reconciliationMutation(
        run.public_run_reference,
        run.workflow_revision_hash,
        run.waiting.node_id,
        run.waiting.request_base64,
        run.waiting.request_hash,
        run.waiting.intent_state_version,
        createReconcileCommandId(),
        actor,
        evidence,
        determination
      );
      const prepared = await mutationJournal.prepare(mutation);
      if (prepared.kind !== "reconciliation") {
        throw new Error(runPageCopy.exactRequestWrongKind);
      }
      pendingReconciliation = prepared;
      reconciliationAccepted = false;
      await deliverReconciliation(mutation);
      await focusAfterReconciliationDelivery();
    } catch (error) {
      if (mutation !== null) {
        await recordReconciliationFailure(mutation.mutation_id, error);
      }
      reconciliationFailureMessage = error instanceof Error
        ? error.message
        : runPageCopy.reconciliation.unconfirmed;
    } finally {
      reconciliationBusy = false;
      if (reconciliationFailureMessage !== null) {
        await tick();
        if (pendingReconciliation !== null) {
          reconciliationActionCard?.focusRetry();
        } else {
          reconciliationActionCard?.focusInput();
        }
      }
    }
  }

  async function retryReconciliation(): Promise<void> {
    if (pendingReconciliation === null) return;
    reconciliationBusy = true;
    reconciliationFailureMessage = null;
    try {
      await deliverReconciliation(pendingReconciliation);
      await focusAfterReconciliationDelivery();
    } catch (error) {
      await recordReconciliationFailure(pendingReconciliation.mutation_id, error);
      reconciliationFailureMessage = error instanceof Error
        ? error.message
        : runPageCopy.exactRetryUnconfirmed;
    } finally {
      reconciliationBusy = false;
      if (reconciliationFailureMessage !== null) {
        await tick();
        reconciliationActionCard?.focusRetry();
      }
    }
  }

  async function discardReconciliation(): Promise<void> {
    if (pendingReconciliation === null) return;
    await mutationJournal.discard(pendingReconciliation.mutation_id);
    pendingReconciliation = null;
    reconciliationAccepted = false;
    reconciliationFailureMessage = null;
    await tick();
    reconciliationActionCard?.focusInput();
  }

  async function deliverReconciliation(mutation: ReconciliationMutation): Promise<void> {
    const result = await cockpitApi.reconcile(mutation);
    const resolved = await mutationJournal.resolve(mutation.mutation_id, {
      type: "reconciliation_response",
      status: result.status,
      target: mutation.target,
      request_body_base64: mutation.body_base64
    });
    const eventAlreadyProvedDecision = matchingReconciliationEventExists(mutation);
    if (result.status === 200 && !resolved && !eventAlreadyProvedDecision) {
      throw new Error(runPageCopy.reconciliationResponseUnproven);
    }
    if (result.status === 202 && resolved) {
      throw new Error(runPageCopy.pendingDecisionTreatedComplete);
    }
    requireRequestedRun(result.value);
    const revision = snapshot.confirmed?.revision;
    if (revision === undefined) throw new Error(runPageCopy.boundWorkflowUnavailable);
    requireBoundRevision(result.value, revision);
    if (eventAlreadyProvedDecision) {
      pendingReconciliation = null;
      reconciliationAccepted = false;
      await load();
      return;
    }
    snapshot = confirmRead(snapshot, snapshot.generation, { run: result.value, revision });
    if (result.status === 202) {
      requireMatchingPendingCommand(result.value, mutation);
      let uncertain: JournalEntry;
      try {
        uncertain = await mutationJournal.markUncertain(mutation.mutation_id);
      } catch (error) {
        if (
          matchingReconciliationEventExists(mutation) &&
          await mutationJournal.get(mutation.mutation_id) === null
        ) {
          pendingReconciliation = null;
          reconciliationAccepted = false;
          await load();
          return;
        }
        throw error;
      }
      if (uncertain.kind !== "reconciliation") {
        throw new Error(runPageCopy.acceptedRequestChangedKind);
      }
      pendingReconciliation = uncertain;
      reconciliationAccepted = true;
    } else {
      pendingReconciliation = null;
      reconciliationAccepted = false;
    }
  }

  function requireMatchingPendingCommand(run: Run, mutation: ReconciliationMutation): void {
    if (run.state !== "WAITING_RECONCILIATION" || run.waiting.type !== "WAITING_RECONCILIATION") {
      throw new Error(runPageCopy.acceptedDecisionUnbound);
    }
    const command = reconciliationCommand(mutation);
    if (
      run.waiting.pending_command === null ||
      !pendingCommandMatches(run.waiting.pending_command, command)
    ) {
      throw new Error(runPageCopy.acceptedCommandDiffers);
    }
  }

  function pendingCommandMatches(
    pending: NonNullable<Extract<Run["waiting"], { type: "WAITING_RECONCILIATION" }>["pending_command"]>,
    command: ReconciliationCommand
  ): boolean {
    if (
      pending.command_id !== command.command_id ||
      pending.actor !== command.actor ||
      pending.evidence !== command.evidence ||
      pending.state !== "PENDING" ||
      pending.determination.type !== command.determination.type
    ) {
      return false;
    }
    if (
      pending.determination.type === "operator_found" &&
      command.determination.type === "operator_found"
    ) {
      return pending.determination.effect_id === command.determination.effect_id &&
        pending.determination.result_base64 === command.determination.result_base64;
    }
    return pending.determination.type === "operator_authoritative_absence" &&
      command.determination.type === "operator_authoritative_absence";
  }

  function matchingReconciliationEventExists(mutation: ReconciliationMutation): boolean {
    const command = reconciliationCommand(mutation);
    return projection?.events.some((event) => {
      if (
        event.event !== "ACTION_RECONCILIATION_RESOLVED" ||
        event.public_run_reference !== publicReference ||
        event.workflow_revision_hash !== mutation.workflow_revision_hash ||
        event.node_id !== mutation.node_id ||
        event.receipt.reconcile_command_id !== command.command_id ||
        event.receipt.request_hash !== mutation.request_hash
      ) {
        return false;
      }
      if (command.determination.type === "operator_found") {
        return event.receipt.confirmation_source === "OPERATOR_FOUND" &&
          event.receipt.effect_id === command.determination.effect_id &&
          event.receipt.result_base64 === command.determination.result_base64 &&
          event.receipt.result_hash === mutation.result_hash;
      }
      return event.receipt.confirmation_source === "OPERATOR_AUTHORIZED_EXECUTION";
    }) ?? false;
  }

  async function recordReconciliationFailure(mutationId: string, error: unknown): Promise<void> {
    if (error instanceof CockpitRequestError && error.definitive_failure) {
      await mutationJournal.discard(mutationId);
      pendingReconciliation = null;
      reconciliationAccepted = false;
      return;
    }
    const entry = await mutationJournal.get(mutationId);
    if (entry?.kind === "reconciliation") {
      const uncertain = await mutationJournal.markUncertain(mutationId);
      if (uncertain.kind === "reconciliation") pendingReconciliation = uncertain;
      reconciliationAccepted = false;
    }
  }

  async function focusAfterReconciliationDelivery(): Promise<void> {
    await tick();
    if (pendingReconciliation !== null && reconciliationAccepted) {
      reconciliationActionCard?.focusStatus();
    } else if (pendingReconciliation === null) {
      await focusRunState();
    }
  }

  async function focusAfterDelivery(): Promise<void> {
    await tick();
    if (pendingWait !== null && waitAccepted) {
      humanActionCard?.focusStatus();
    } else if (pendingWait === null) {
      await focusRunState();
    }
  }

  async function focusWaitFailure(): Promise<void> {
    await tick();
    if (pendingWait !== null) {
      humanActionCard?.focusRetry();
    } else {
      humanActionCard?.focusInput();
    }
  }

  async function focusRunState(): Promise<void> {
    await tick();
    runStateElement?.focus();
  }

  async function deliverWait(mutation: WaitMutation): Promise<void> {
    const result = await cockpitApi.answer(mutation);
    const resolved = await mutationJournal.resolve(mutation.mutation_id, {
      type: "wait_response",
      status: result.status,
      target: mutation.target,
      request_body_base64: mutation.body_base64
    });
    const eventAlreadyProvedAnswer = matchingWaitEventExists(mutation);
    if (result.status === 200 && !resolved && !eventAlreadyProvedAnswer) {
      throw new Error(runPageCopy.answerResponseUnproven);
    }
    if (result.status === 202 && resolved) {
      throw new Error(runPageCopy.pendingAnswerTreatedComplete);
    }
    requireRequestedRun(result.value);
    if (isRunV3(result.value)) {
      throw new Error(runPageCopy.answerResponseWrongFormat);
    }
    const answered = result.value;
    const revision = snapshot.confirmed?.revision;
    if (revision === undefined) throw new Error(runPageCopy.boundWorkflowUnavailable);
    requireBoundRevision(answered, revision);
    if (eventAlreadyProvedAnswer) {
      pendingWait = null;
      waitAccepted = false;
      await load();
      return;
    }
    snapshot = confirmRead(snapshot, snapshot.generation, { run: answered, revision });
    if (result.status === 202) {
      let uncertain: JournalEntry;
      try {
        uncertain = await mutationJournal.markUncertain(mutation.mutation_id);
      } catch (error) {
        if (matchingWaitEventExists(mutation) && await mutationJournal.get(mutation.mutation_id) === null) {
          pendingWait = null;
          waitAccepted = false;
          await load();
          return;
        }
        throw error;
      }
      if (uncertain.kind !== "wait") throw new Error(runPageCopy.acceptedRequestChangedKind);
      pendingWait = uncertain;
      waitAccepted = true;
    } else {
      pendingWait = null;
      waitAccepted = false;
    }
  }

  function matchingWaitEventExists(mutation: WaitMutation): boolean {
    const answer = waitAnswer(mutation);
    return projection?.events.some(
      (event) =>
        event.event === "WAIT_ANSWERED" &&
        "answer" in event &&
        event.public_run_reference === mutation.public_run_reference &&
        event.workflow_revision_hash === mutation.workflow_revision_hash &&
        event.node_id === mutation.node_id &&
        event.answer === answer &&
        event.answer_hash === mutation.answer_hash
    ) ?? false;
  }

  async function recordWaitFailure(mutationId: string, error: unknown): Promise<void> {
    if (error instanceof CockpitRequestError && error.definitive_failure) {
      await mutationJournal.discard(mutationId);
      pendingWait = null;
      waitAccepted = false;
      return;
    }
    const entry = await mutationJournal.get(mutationId);
    if (entry?.kind === "wait") {
      const uncertain = await mutationJournal.markUncertain(mutationId);
      if (uncertain.kind === "wait") pendingWait = uncertain;
      waitAccepted = false;
    }
  }

  function exactEvent(event: RunEvent): string {
    const bytes = projection?.payload_bytes_by_cursor.get(event.cursor);
    return bytes === undefined ? JSON.stringify(event) : new globalThis.TextDecoder().decode(bytes);
  }

  function keyboardScrollableEventEvidence(region: HTMLElement): void {
    region.tabIndex = 0;
  }

  /** Only a V3 document declares a name (#506): a V1 or V2 revision has none to read. */
  $: workflowName =
    snapshot.confirmed !== null && snapshot.confirmed.revision.graph.workflow_format_version === 3
      ? snapshot.confirmed.revision.graph.name
      : null;
  $: headerTitle = runHeaderTitle(workflowName);
</script>

<section aria-labelledby={v3Run !== null ? "v3-run-title" : "run-title"}>
  <BackLink label={cameFrom.label} path={cameFrom.path} {navigate} />

  {#if v3Run !== null}
    <V3RunView
      run={v3Run}
      {cockpitApi}
      {mutationJournal}
      {projection}
      onRunRead={(read) => {
        v3Run = read;
      }}
      onRetryStream={retryStream}
    />
  {:else if snapshot.request.state === "failed"}
    <ProblemNotice problem={snapshot.request.failure} />
  {:else if failureMessage !== null}
    <ProblemNotice title={runPageCopy.runUnavailable} message={failureMessage} />
  {/if}

  {#if snapshot.confirmed !== null}
    <header class="run-header">
      <div>
        <h1 id="run-title">{headerTitle}</h1>
        <p class="run-identity">
          <ProofAnchor
            label={runHeaderCopy.runIdLabel}
            seals={runHeaderCopy.sealsRunId}
            value={snapshot.confirmed.run.run_id}
          />
        </p>
      </div>
    </header>

    {#if snapshot.request.state === "loading"}<p class="status compact-status" role="status">{runPageCopy.refreshing}</p>{/if}

    {#if projection !== null}
      <!-- A dropped-but-recovering stream (connection "reconnecting", no
           protocol or terminal problem) is exactly the generic reachability
           loss the central connection store already names once, above every
           room (#700) -- this status line only ever speaks for what is
           specific to this stream: still connecting, live, complete, or a
           real protocol/terminal failure. -->
      {#if streamStopped(projection) || projection.connection !== "reconnecting"}
        <p class="connection connection-{projection.connection}" class:connection-problem={streamStopped(projection)} role="status">
          <span aria-hidden="true">{streamStopped(projection) ? "◇" : projection.connection === "complete" ? "✓" : projection.connection === "live" ? "●" : "↻"}</span>
          {connectionLabel(projection)}
        </p>
      {/if}
      {#if streamStopped(projection)}
        <button
          class="quiet"
          type="button"
          disabled={snapshot.request.state === "loading"}
          onclick={retryStream}
        >{runPageCopy.retry}</button>
      {/if}
      {#if projection.stream_failure !== null}
        <ProblemNotice problem={projection.stream_failure} />
      {:else if protocolTitle(projection) !== null}
        <ProblemNotice title={protocolTitle(projection) ?? runPageCopy.eventInvalid} message={protocolDetail(projection) ?? ""} />
      {/if}
      {#if snapshot.confirmed.run.state === "STARTED"}
        <p class="honest-absence">{wrapDisplayCopy(runPageCopy.processLogInLease)}</p>
      {/if}
    {/if}

    <dl class="run-summary">
      <div><dt>{runPageCopy.state}</dt><dd tabindex="-1" bind:this={runStateElement} data-testid="run-state">{snapshot.confirmed.run.state.replaceAll("_", " ").toLowerCase()}</dd></div>
      <div>
        <dt>{wrapDisplayCopy(runPageCopy.workflowRevision)}</dt>
        <dd>
          <ProofAnchor
            label={wrapDisplayCopy(runPageCopy.workflowRevision)}
            seals={runPageCopy.sealsWorkflow}
            value={snapshot.confirmed.run.workflow_revision_hash}
          />
        </dd>
      </div>
      {#if snapshot.confirmed.run.terminal_hash !== null}
        <div>
          <dt>{wrapDisplayCopy(runPageCopy.terminalHash)}</dt>
          <dd>
            <ProofAnchor
              label={wrapDisplayCopy(runPageCopy.terminalHash)}
              seals={runPageCopy.sealsTerminal}
              value={snapshot.confirmed.run.terminal_hash}
            />
          </dd>
        </div>
      {/if}
    </dl>

    {#if snapshot.confirmed.run.state === "WAITING_INPUT" && snapshot.confirmed.run.waiting.type === "WAITING_INPUT"}
      <HumanActionCard
        bind:this={humanActionCard}
        pending={pendingWait}
        {pendingAnswer}
        accepted={waitAccepted}
        busy={waitBusy}
        validationMessage={waitValidationMessage}
        failureMessage={waitFailureMessage}
        onAnswer={(answer) => { void submitWait(answer); }}
        onRetry={() => { void retryWait(); }}
        onDiscard={() => { void discardWait(); }}
      />
    {/if}

    {#if snapshot.confirmed.run.state === "WAITING_RECONCILIATION" && snapshot.confirmed.run.waiting.type === "WAITING_RECONCILIATION"}
      <ReconciliationActionCard
        bind:this={reconciliationActionCard}
        waiting={snapshot.confirmed.run.waiting}
        pending={pendingReconciliation}
        accepted={reconciliationAccepted}
        busy={reconciliationBusy}
        failureMessage={reconciliationFailureMessage}
        onResolve={submitReconciliation}
        onRetry={() => { void retryReconciliation(); }}
        onDiscard={() => { void discardReconciliation(); }}
      />
    {/if}

    <NodeRail
      run={snapshot.confirmed.run}
      graph={executableGraph(snapshot.confirmed.revision.graph)}
      events={projection?.events ?? []}
      agentOutputs={projection?.agent_outputs_by_cursor ?? new Map()}
      {openFormNodeIds}
    />

    <details class="event-log" open={snapshot.confirmed.run.state === "STARTED"}>
      <summary>{runPageCopy.events} <span>{projection?.events.length ?? 0}</span></summary>
      {#if (projection?.events.length ?? 0) === 0}
        <p class="empty-event">{runPageCopy.noDurableEvents}</p>
      {:else}
        <ol>
          {#each projection?.events ?? [] as event (event.cursor)}
            <li>
              <span><strong>{event.event.replaceAll("_", " ")}</strong><small>#{event.sequence} · {event.node_id}</small></span>
              <ProofAnchor
                label={`${event.event.replaceAll("_", " ")} #${event.sequence}`}
                seals={runPageCopy.sealsEvent}
                value={event.event_hash}
              />
              <div
                class="event-evidence"
                role="region"
                use:keyboardScrollableEventEvidence
                aria-label={`${wrapDisplayCopy(runPageCopy.eventEvidence)} #${event.sequence}`}
              >
                <pre>{exactEvent(event)}</pre>
              </div>
            </li>
          {/each}
        </ol>
      {/if}
    </details>
  {:else if snapshot.request.state === "loading"}
    <p class="status" role="status">{runPageCopy.looking}</p>
  {:else if v3Run === null}
    <button type="button" onclick={load}>{runPageCopy.retry}</button>
  {/if}
</section>
