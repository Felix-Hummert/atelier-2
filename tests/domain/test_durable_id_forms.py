"""The exact form of every durable identity this engine mints.

A prefix constant proves only that a name starts a certain way. What the store,
the engine and every operator tool key on is the whole string, and its shape is
a durable fact: change the digest, the separator or the encoding and identical
work becomes a second identity — silently, with nothing red.

This table pins whole vectors rather than prefixes, for every one of the ten
derivations the engine has, for the round dimension a declared loop turns, and
for the surrounding space and non-ASCII input that separate an exact identity
from a normalised one. A prefix test stays green
while the tail drifts; a vector test cannot. Every value below was computed from
the production owner and pasted, never hand-derived here: a test that recomputes
the form it is checking would agree with any form at all. That is also what lets the table survive a
move — an owner may change address, and the bytes may not.

The ten do not share one derivation, and this table deliberately does not
make them: three schemas live here side by side (framed digest, bare digest,
plain concatenation) because unifying them would restart every durable workflow
under a new identity.

The round follows the same rule from the other side. The first round of a node
is pinned here to be the *same bytes* as the roundless derivation that landed
before loops existed, and every later round is its own value — so a document
that declares no loop cannot have moved a single stored identity, and two rounds
of one node can never be mistaken for one execution.
"""

from __future__ import annotations

import pytest

from atelier2.adapters.dbos.workflow_ids import (
    action_continuation_workflow_id_for,
    answer_workflow_id_for,
    bootstrap_workflow_id_for,
    cancellation_workflow_id_for,
    effect_workflow_id_for,
    node_workflow_id_for,
    reconcile_workflow_id_for,
    replacement_workflow_id_for,
    subworkflow_workflow_id_for,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    AgentAttemptReplacement,
    CancelAgentAttemptRequest,
)
from atelier2.contracts.effects import LogicalEffectKey, ReconcileCommandId
from atelier2.contracts.executions import NodeExecutionId, logical_effect_key_for
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    WorkflowRevisionHash,
)

RUN = RunId("run-1")
REVISION = WorkflowRevisionHash("2" * 64)
EXECUTION = NodeExecutionId.for_node(RUN, REVISION, "node")


def executed_in_round(round_ordinal: int) -> NodeExecutionId:
    return NodeExecutionId.for_node(RUN, REVISION, "node", round_ordinal)


EFFECT_KEY = LogicalEffectKey("atelier2-node-effect-" + "d" * 64)
RECONCILE_COMMAND = ReconcileCommandId("command-1")
ATTEMPT = AgentAttemptId("a" * 64)
CANCELLATION = CancelAgentAttemptRequest(
    RUN, ATTEMPT, "command-1", 0, AgentAttemptReplacement.NONE
)

DURABLE_ID_VECTORS = (
    pytest.param(
        bootstrap_workflow_id_for(RUN),
        "atelier2-run-4e65d3fbe8ad6535681b021b30785b12b6c0e3f8878859a4148b3f58b8835db0",
        id="bootstrap-run",
    ),
    pytest.param(
        bootstrap_workflow_id_for(RunId(" run-1 ")),
        "atelier2-run-2dab30c2369645f3034a977f79499de4115eb9680bef84680f4cecc981e67ee7",
        id="bootstrap-run-padded",
    ),
    pytest.param(
        bootstrap_workflow_id_for(RunId("\N{SNOWMAN}")),
        "atelier2-run-51643361c79ecaef25a8de802de24f570ba25d9c2df1d22d94fade11b4f466cc",
        id="bootstrap-run-non-ascii",
    ),
    pytest.param(
        node_workflow_id_for(EXECUTION),
        "atelier2-node-d28220985ea061772c063c1c5480ed6179d99c080233750bee4b92849fe54c74",
        id="node",
    ),
    pytest.param(
        answer_workflow_id_for(EXECUTION),
        "atelier2-answer-"
        "cbabf8e7d67b0e006e39ba394f1d46bcbf073278eb24b0990a2d1dc1143667a5",
        id="answer",
    ),
    pytest.param(
        subworkflow_workflow_id_for(EXECUTION),
        "atelier2-subworkflow-"
        "3da39105be881bffe53d6ef6721f1879a5e8c0d1c1b36883ab4dc6b2f95992d0",
        id="subworkflow",
    ),
    pytest.param(
        logical_effect_key_for(EXECUTION).value,
        "atelier2-node-effect-"
        "d290b70bcca6edc562a24e9239b41b59220335f612f2bf7d20b1b0982d87a5c9",
        id="logical-effect-key",
    ),
    pytest.param(
        effect_workflow_id_for(EFFECT_KEY),
        "atelier2-effect-"
        "4cfeb7f5fd173c7a8880b57bdc7f8297e55aadf317d6988678364d6f6b84a5ec",
        id="effect",
    ),
    pytest.param(
        effect_workflow_id_for(LogicalEffectKey(" run-1/action-1 ")),
        "atelier2-effect-"
        "c5f618be5af7f6dbfd72e38fdcd8d8d489db73c420ccedae3644fa4e8d1e0211",
        id="effect-padded-logical-key",
    ),
    pytest.param(
        action_continuation_workflow_id_for(EFFECT_KEY),
        "atelier2-action-continuation-"
        "6e5509ed88636f4b517f2d9a1df10e1ab2b2888be25878988131308f604ed0c2",
        id="action-continuation",
    ),
    pytest.param(
        reconcile_workflow_id_for(RECONCILE_COMMAND),
        "atelier2-reconcile-"
        "e12c157c0051f399d6ecb3c03a191d6dd199f58abcb4cf1894db2fc30f331ddf",
        id="reconcile",
    ),
    pytest.param(
        reconcile_workflow_id_for(ReconcileCommandId(" command-1 ")),
        "atelier2-reconcile-"
        "ed130bd4e8308c690a08d2e099d0b5394042153082c760870f9cf3a19cc00483",
        id="reconcile-padded-command",
    ),
    pytest.param(
        cancellation_workflow_id_for(CANCELLATION),
        "atelier2-agent-cancel-"
        "efe2df443d8ee5df44a8bce70199c036ee782f3b2c87cdd5aaece8a2fd4f9fa1",
        id="agent-cancellation",
    ),
    pytest.param(
        replacement_workflow_id_for(ATTEMPT),
        "atelier2-agent-replacement-" + "a" * 64,
        id="agent-replacement",
    ),
    pytest.param(
        executed_in_round(FIRST_ROUND_ORDINAL).value,
        EXECUTION.value,
        id="node-execution-first-round-is-the-landed-form",
    ),
    pytest.param(
        executed_in_round(2).value,
        "647f77d7df3f68f6b00ce092d8423cfb9cc2c01292a87366d170f3b441406c4e",
        id="node-execution-second-round",
    ),
    pytest.param(
        executed_in_round(3).value,
        "977fa1fb75d7b406ed986858f0afffde8932484d9357ff4e8b3bcf52d5e1c69c",
        id="node-execution-third-round",
    ),
    pytest.param(
        executed_in_round(10).value,
        "f197a38b6bb345a5784d39f1b20a969ce70770b6f8fbe08731a12d93132d17be",
        id="node-execution-tenth-round",
    ),
    pytest.param(
        node_workflow_id_for(executed_in_round(2)),
        "atelier2-node-"
        "e8d6ffba5c33358acc3db881eb00c9b78303707290f2e6173c875578b3d8f024",
        id="node-workflow-of-a-second-round",
    ),
)


@pytest.mark.proves("a-durable-id-keeps-the-exact-form-it-was-minted-in")
@pytest.mark.proves(
    "every-durable-workflow-id-keeps-its-exact-form-when-its-owner-moves"
)
@pytest.mark.parametrize(("minted", "exact_form"), DURABLE_ID_VECTORS)
def test_a_durable_id_keeps_the_exact_form_it_was_minted_in(
    minted: str, exact_form: str
) -> None:
    assert minted == exact_form
