from __future__ import annotations

from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
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
