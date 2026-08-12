from __future__ import annotations

from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.ports.effects import EffectAdapterFactory


def exact_output_runtime(
    settings: DbosRuntimeSettings, effect_adapter_factory: EffectAdapterFactory
) -> DbosRuntime:
    return DbosRuntime(
        settings,
        effect_adapter_factory,
        ExactOutputAgentExecutorFactory(),
    )
