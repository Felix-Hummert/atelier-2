from __future__ import annotations

from collections.abc import Callable

import pytest

from atelier2.adapters.dbos.runtime import (
    DbosRuntime,
    DbosRuntimeBindingConflict,
    DbosRuntimeSettings,
)
from atelier2.ports.effects import EffectAdapterFactory
from tests.scenarios.agents import RecordingAgentExecutorFactoryV2
from tests.scenarios.runs import (
    V3_EXECUTOR_REVISION,
    V3_OPERATIONAL_IDENTITY,
    V3_PROVIDER,
)


def recording_exact_runtime(
    settings: DbosRuntimeSettings,
    effect_adapter_factory: EffectAdapterFactory,
    provider_output: bytes,
) -> DbosRuntime:
    """The production runtime serving one recording `exact` executor for V3 runs.

    Every V3 scenario that lets the runtime execute an agent node binds the
    provider `publish_v3_agent_bindings` publishes, so the factory identity
    lives with those bindings rather than being restated per file.
    """
    return DbosRuntime(
        settings,
        effect_adapter_factory,
        (
            RecordingAgentExecutorFactoryV2(
                V3_PROVIDER.value,
                V3_EXECUTOR_REVISION.value,
                V3_OPERATIONAL_IDENTITY,
                provider_output,
            ),
        ),
    )


def binding_refusal_of(
    open_runtime: Callable[[], DbosRuntime],
) -> DbosRuntimeBindingConflict:
    """The binding refusal this open owes, holding nothing when it fails to come.

    A process may own one runtime binding, so an open that is expected to be
    refused and instead succeeds hands its runtime to nobody: every later open
    in that process is then refused for a binding no test still names, and the
    test that arranged the refusal is not the one that fails. Closing what
    unexpectedly opened keeps the failure where its arrangement is.
    """

    try:
        runtime = open_runtime()
    except DbosRuntimeBindingConflict as refusal:
        return refusal
    runtime.close()
    pytest.fail("opening this runtime was expected to be refused")
