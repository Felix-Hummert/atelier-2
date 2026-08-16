"""The exact form of every durable id a prefix owner mints.

A prefix constant proves only that a name starts a certain way. What the store,
the engine and every operator tool key on is the whole string, and its shape is
a durable fact: change the digest, the separator or the encoding and identical
work becomes a second identity — silently, with nothing red.

So these pin whole vectors rather than prefixes. A prefix test stays green while
the tail drifts; a vector test cannot. Every value below was computed from the
production owner and pasted, never hand-derived here: a test that recomputes the
form it is checking would agree with any form at all.
"""

from __future__ import annotations

import pytest

from atelier2.adapters.dbos.advancer import effect_workflow_id_for
from atelier2.adapters.dbos.reconciler import reconcile_workflow_id_for
from atelier2.adapters.dbos.starter import bootstrap_workflow_id_for
from atelier2.contracts.effects import LogicalEffectKey, ReconcileCommandId
from atelier2.contracts.executions import NodeExecutionId, logical_effect_key_for
from atelier2.contracts.runs import RunId, WorkflowRevisionHash

RUN = RunId("run-1")
EXECUTION = NodeExecutionId.for_node(RUN, WorkflowRevisionHash("2" * 64), "node")
EFFECT_KEY = LogicalEffectKey("atelier2-node-effect-" + "d" * 64)


@pytest.mark.proves("a-durable-id-keeps-the-exact-form-it-was-minted-in")
def test_a_bootstrap_workflow_id_keeps_its_exact_form() -> None:
    assert bootstrap_workflow_id_for(RUN) == (
        "atelier2-run-4e65d3fbe8ad6535681b021b30785b12b6c0e3f8878859a4148b3f58b8835db0"
    )


@pytest.mark.proves("a-durable-id-keeps-the-exact-form-it-was-minted-in")
def test_a_logical_effect_key_keeps_its_exact_form() -> None:
    assert logical_effect_key_for(EXECUTION) == LogicalEffectKey(
        "atelier2-node-effect-"
        "d290b70bcca6edc562a24e9239b41b59220335f612f2bf7d20b1b0982d87a5c9"
    )


@pytest.mark.proves("a-durable-id-keeps-the-exact-form-it-was-minted-in")
def test_an_effect_workflow_id_keeps_its_exact_form() -> None:
    assert effect_workflow_id_for(EFFECT_KEY) == (
        "atelier2-effect-"
        "4cfeb7f5fd173c7a8880b57bdc7f8297e55aadf317d6988678364d6f6b84a5ec"
    )


@pytest.mark.proves("a-durable-id-keeps-the-exact-form-it-was-minted-in")
def test_a_reconcile_workflow_id_keeps_its_exact_form() -> None:
    assert reconcile_workflow_id_for(ReconcileCommandId("command-1")) == (
        "atelier2-reconcile-"
        "e12c157c0051f399d6ecb3c03a191d6dd199f58abcb4cf1894db2fc30f331ddf"
    )
