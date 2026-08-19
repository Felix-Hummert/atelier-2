"""How many concurrent fake-executor runs one SQLite instance carries.

This is a measurement, not a capacity promise. It drives the served start
door, the event chain, and one SSE reader per run against the current SQLite
file, using the in-process fake executor rather than a billed provider. Every
door answers with a named outcome; a traceback is a harness defect.

CI stays at two concurrent runs so the suite stays cheap. A larger local sweep
is the operator command OPERATIONS.md names; it reuses this file.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import resource
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import (
    SQLITE_LOCK_TIMEOUT_SECONDS,
    DbosRuntime,
    DbosRuntimeSettings,
)
from atelier2.adapters.dbos.starter import DbosWorkflowRevisionPublisher
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.openapi import API_PREFIX
from atelier2.api.problems import PROBLEM_TYPE_PREFIX
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.runs import RunState, WorkflowRevision
from atelier2.host.serving import (
    MAXIMUM_QUERY_ADMISSION_WAIT_MILLISECONDS,
    api_limits,
    event_poll_backoff,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
)
from tests.scenarios.api import durable_asgi_app
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

LOAD_CONCURRENCY_ENVIRONMENT = "ATELIER2_LOAD_CONCURRENCY"
CI_CONCURRENCY = 2
DRIVER_DEADLINE_SECONDS = 8
"""The poll budget the V3 driver tests already wait; CI reuses it.

A larger sweep may spend the instance's SQLite lock timeout on the start door
before that same budget, because a contended write is allowed to wait that
long.
"""
NAMED_OUTCOME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SUCCESS_OUTCOMES = frozenset({"accepted", "completed"})
RUN_PATH = API_PREFIX + "/runs"
PROVIDER_OUTPUT = b"true"
QUERY_ADMISSION_WAIT_SECONDS = MAXIMUM_QUERY_ADMISSION_WAIT_MILLISECONDS / 1_000


class Door(StrEnum):
    START = "start-door"
    EVENT_WRITE = "event-write"
    SSE_READER = "sse-reader"


class ObservedCause(StrEnum):
    WRITER_LOCK = "writer-lock"
    QUERY_ADMISSION = "query-admission"
    PROCESS_SPAWN = "process-spawn"
    WATCHDOG_CGROUP = "watchdog-cgroup"
    MEMORY = "memory"
    NOT_OBSERVED = "not-observed"


@dataclass(frozen=True)
class DoorSample:
    door: Door
    outcome: str
    latency_seconds: float
    observed_at: float
    cause: ObservedCause
    detail: str


@dataclass(frozen=True)
class LoadReport:
    concurrency: int
    completed_runs: int
    failed_runs: int
    samples: tuple[DoorSample, ...]
    first_observed_pressure: DoorSample
    peak_rss_kib: int
    sqlite_lock_timeout_seconds: float
    query_admission_wait_seconds: float


def requested_concurrency() -> int:
    raw = os.environ.get(LOAD_CONCURRENCY_ENVIRONMENT)
    if raw is None:
        return CI_CONCURRENCY
    if not raw.isdigit() or raw == "0" or (len(raw) > 1 and raw.startswith("0")):
        raise ValueError(
            f"{LOAD_CONCURRENCY_ENVIRONMENT} must be a positive decimal integer"
        )
    return int(raw)


def completion_deadline_seconds(concurrency: int) -> float:
    if concurrency <= CI_CONCURRENCY:
        return DRIVER_DEADLINE_SECONDS
    return SQLITE_LOCK_TIMEOUT_SECONDS + DRIVER_DEADLINE_SECONDS


def publish_one_agent(runtime: DbosRuntime) -> tuple[str, str]:
    published = DbosCatalogStore(runtime.engine).publish_revision(ANY_JSON_SCHEMA)
    assert isinstance(
        published, (PublishedRevisionCreated, PublishedRevisionExisting)
    ), published
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
    )
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("exact/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(configuration),
        AgentConfigurationRevisionCreated,
    )
    workflow = WorkflowRevision(
        b"""format_version: 3
name: Load measurement
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Finish this load sample.
"""
        + declared_output()
    )
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow.revision_hash.value, configuration.revision_hash.value


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    recording = RecordingAgentExecutorFactoryV2(
        "exact", "exact/v1", "exact-operation", PROVIDER_OUTPUT
    )
    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "sqlite-load-measurement",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        ExactOutputAgentExecutorFactory(),
        (recording,),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def problem_code(body: Mapping[str, object]) -> str:
    type_uri = body.get("type")
    if not isinstance(type_uri, str) or not type_uri.startswith(PROBLEM_TYPE_PREFIX):
        return "unreadable-response"
    code = type_uri.removeprefix(PROBLEM_TYPE_PREFIX)
    return code or "unreadable-response"


def classify_cause(
    outcome: str, detail: str, latency_seconds: float, door: Door
) -> ObservedCause:
    lowered = detail.lower()
    if "memoryerror" in lowered:
        return ObservedCause.MEMORY
    if any(
        token in lowered
        for token in ("emfile", "too many open files", "eagain", "cannot allocate")
    ):
        return ObservedCause.PROCESS_SPAWN
    if "cgroup" in lowered or "watchdog" in lowered:
        return ObservedCause.WATCHDOG_CGROUP
    if outcome == "temporarily-unavailable":
        if latency_seconds >= SQLITE_LOCK_TIMEOUT_SECONDS:
            return ObservedCause.WRITER_LOCK
        if latency_seconds >= QUERY_ADMISSION_WAIT_SECONDS:
            return ObservedCause.QUERY_ADMISSION
    if outcome == "sse-ended-without-terminal" and door is Door.SSE_READER:
        # stream.py ends a live reader this way for query-admission backpressure
        # or a temporarily-unavailable read; the frame does not say which.
        return ObservedCause.NOT_OBSERVED
    return ObservedCause.NOT_OBSERVED


def read_json(response: httpx.Response) -> Mapping[str, object]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def parse_sse_frames(body: str) -> tuple[tuple[Mapping[str, object], ...], str | None]:
    frames: list[Mapping[str, object]] = []
    failure: str | None = None
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        data_lines = [
            line[len("data:") :].lstrip()
            for line in block.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            return tuple(frames), "unreadable-response"
        if not isinstance(payload, dict):
            return tuple(frames), "unreadable-response"
        if payload.get("event") == "STREAM_FAILED":
            problem = payload.get("problem")
            failure = (
                problem_code(problem)
                if isinstance(problem, dict)
                else "unreadable-response"
            )
            continue
        frames.append(payload)
    return tuple(frames), failure


async def drive_one_run(
    client: AsyncClient,
    revision_hash: str,
    configuration_hash: str,
    index: int,
    rendezvous: asyncio.Barrier,
) -> tuple[DoorSample, DoorSample, DoorSample]:
    await rendezvous.wait()
    started_at = time.monotonic()
    try:
        start = await client.post(
            RUN_PATH,
            json={
                "workflow_format_version": 3,
                "run_id": f"load/{index}",
                "workflow_revision_hash": revision_hash,
                "agent_bindings": [
                    {
                        "role": "builder",
                        "agent_configuration_revision_hash": configuration_hash,
                    }
                ],
                "orders": [],
            },
            headers={"content-type": "application/json"},
        )
    except httpx.TimeoutException:
        start_sample = _sample(
            Door.START, "transport-timeout", started_at, "httpx timed out the start"
        )
        return (
            start_sample,
            _refused(Door.EVENT_WRITE, started_at),
            _refused(Door.SSE_READER, started_at),
        )
    start_body = read_json(start)
    if start.status_code not in {HTTPStatus.CREATED, HTTPStatus.OK}:
        outcome = problem_code(start_body) if start_body else "unreadable-response"
        detail = str(start_body.get("detail", start.text))
        start_sample = _sample(Door.START, outcome, started_at, detail)
        return (
            start_sample,
            _refused(Door.EVENT_WRITE, started_at),
            _refused(Door.SSE_READER, started_at),
        )
    start_sample = _sample(Door.START, "accepted", started_at, "")
    reference = start_body.get("public_run_reference")
    if not isinstance(reference, str) or not reference:
        missing = _sample(
            Door.EVENT_WRITE,
            "unreadable-response",
            started_at,
            "start omitted the public run reference",
        )
        return (
            start_sample,
            missing,
            _sample(
                Door.SSE_READER,
                "unreadable-response",
                started_at,
                "start omitted the public run reference",
            ),
        )
    events_path = f"{RUN_PATH}/{reference}/events"
    sse_started = time.monotonic()
    try:
        sse = await client.get(events_path, headers={"accept": "text/event-stream"})
    except httpx.TimeoutException:
        sse_sample = _sample(
            Door.SSE_READER,
            "transport-timeout",
            sse_started,
            "httpx timed out the event stream",
        )
        return (
            start_sample,
            await _run_state_sample(client, reference, started_at),
            sse_sample,
        )
    if sse.status_code != HTTPStatus.OK:
        body = read_json(sse)
        outcome = problem_code(body) if body else "unreadable-response"
        sse_sample = _sample(
            Door.SSE_READER, outcome, sse_started, str(body.get("detail", sse.text))
        )
        return (
            start_sample,
            await _run_state_sample(client, reference, started_at),
            sse_sample,
        )
    frames, stream_failure = parse_sse_frames(sse.text)
    if stream_failure is not None:
        sse_sample = _sample(Door.SSE_READER, stream_failure, sse_started, sse.text)
    elif _saw_terminal(frames):
        sse_sample = _sample(Door.SSE_READER, "completed", sse_started, "")
    else:
        sse_sample = _sample(
            Door.SSE_READER, "sse-ended-without-terminal", sse_started, sse.text
        )
    return (
        start_sample,
        await _run_state_sample(client, reference, started_at),
        sse_sample,
    )


def _saw_terminal(frames: Sequence[Mapping[str, object]]) -> bool:
    return any(
        frame.get("event")
        in {"AGENT_COMPLETED", "AGENT_FAILED", "SUBWORKFLOW_COMPLETED"}
        for frame in frames
    )


async def _run_state_sample(
    client: AsyncClient, reference: str, started_at: float
) -> DoorSample:
    try:
        run = await client.get(f"{RUN_PATH}/{reference}")
    except httpx.TimeoutException:
        return _sample(
            Door.EVENT_WRITE,
            "transport-timeout",
            started_at,
            "httpx timed out the run read",
        )
    body = read_json(run)
    if run.status_code != HTTPStatus.OK:
        outcome = problem_code(body) if body else "unreadable-response"
        return _sample(
            Door.EVENT_WRITE, outcome, started_at, str(body.get("detail", run.text))
        )
    state = body.get("state")
    if state == RunState.COMPLETED.value:
        return _sample(Door.EVENT_WRITE, "completed", started_at, "")
    if state == RunState.FAILED.value:
        return _sample(Door.EVENT_WRITE, "failed", started_at, "")
    if isinstance(state, str) and NAMED_OUTCOME.fullmatch(
        state.lower().replace("_", "-")
    ):
        return _sample(
            Door.EVENT_WRITE,
            "deadline-elapsed",
            started_at,
            f"run stayed {state}",
        )
    return _sample(
        Door.EVENT_WRITE,
        "unreadable-response",
        started_at,
        f"run state was {state!r}",
    )


def _refused(door: Door, started_at: float) -> DoorSample:
    return _sample(
        door, "start-refused", started_at, "the start door did not accept this run"
    )


def _sample(door: Door, outcome: str, started_at: float, detail: str) -> DoorSample:
    observed_at = time.monotonic()
    latency = observed_at - started_at
    return DoorSample(
        door,
        outcome,
        latency,
        observed_at,
        classify_cause(outcome, detail, latency, door),
        detail,
    )


def first_observed_pressure(samples: Sequence[DoorSample]) -> DoorSample:
    named_faults = tuple(
        sample for sample in samples if sample.outcome not in SUCCESS_OUTCOMES
    )
    if named_faults:
        return min(named_faults, key=lambda sample: sample.observed_at)
    return max(samples, key=lambda sample: sample.latency_seconds)


def format_report(report: LoadReport) -> str:
    pressure = report.first_observed_pressure
    lines = [
        f"concurrency={report.concurrency}",
        f"completed_runs={report.completed_runs}",
        f"failed_runs={report.failed_runs}",
        (
            "first_observed_pressure="
            f"{pressure.door.value}:{pressure.outcome}:{pressure.cause.value}"
            f":{pressure.latency_seconds:.3f}s"
        ),
        f"peak_rss_kib={report.peak_rss_kib}",
        (
            "setup=in-process ASGI one event loop, one SQLite file, "
            "V3 one-agent document, RecordingAgentExecutorFactoryV2 fake executor, "
            f"sqlite_lock_timeout={report.sqlite_lock_timeout_seconds}s, "
            f"query_admission_wait={report.query_admission_wait_seconds}s"
        ),
        "unobserved=billed-provider-quota",
    ]
    if pressure.cause is ObservedCause.NOT_OBSERVED:
        lines.append(
            "cause_note=the harness names a door and an outcome; it does not guess "
            "writer-lock, process-spawn, watchdog-cgroup, or memory without a signal"
        )
    by_door: dict[Door, list[DoorSample]] = {door: [] for door in Door}
    for sample in report.samples:
        by_door[sample.door].append(sample)
    for door, door_samples in by_door.items():
        latencies = tuple(sample.latency_seconds for sample in door_samples)
        outcomes = tuple(sample.outcome for sample in door_samples)
        lines.append(
            f"{door.value}: n={len(door_samples)} "
            f"max_latency_s={max(latencies):.3f} outcomes={','.join(outcomes)}"
        )
    return "\n".join(lines)


async def _drive_load(
    app: FastAPI,
    revision_hash: str,
    configuration_hash: str,
    concurrency: int,
) -> LoadReport:
    deadline = completion_deadline_seconds(concurrency)
    before_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport, base_url="http://test", timeout=deadline
    ) as client:
        rendezvous = asyncio.Barrier(concurrency)
        gathered = await asyncio.gather(
            *(
                drive_one_run(
                    client, revision_hash, configuration_hash, index, rendezvous
                )
                for index in range(concurrency)
            ),
            return_exceptions=True,
        )
    observed: list[tuple[DoorSample, DoorSample, DoorSample]] = []
    for result in gathered:
        if isinstance(result, Exception):
            started_at = time.monotonic()
            detail = f"{type(result).__name__}: {result}"
            observed.append(
                (
                    _sample(Door.START, "internal-error", started_at, detail),
                    _refused(Door.EVENT_WRITE, started_at),
                    _refused(Door.SSE_READER, started_at),
                )
            )
            continue
        assert not isinstance(result, BaseException)
        observed.append(result)
    samples = tuple(sample for triple in observed for sample in triple)
    event_writes = tuple(
        sample for sample in samples if sample.door is Door.EVENT_WRITE
    )
    after_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return LoadReport(
        concurrency,
        sum(sample.outcome == "completed" for sample in event_writes),
        sum(sample.outcome == "failed" for sample in event_writes),
        samples,
        first_observed_pressure(samples),
        max(before_rss, after_rss),
        SQLITE_LOCK_TIMEOUT_SECONDS,
        QUERY_ADMISSION_WAIT_SECONDS,
    )


def measure_load(runtime: DbosRuntime, concurrency: int) -> LoadReport:
    revision_hash, configuration_hash = publish_one_agent(runtime)
    app = durable_asgi_app(
        runtime, limits=api_limits(), poll_backoff=event_poll_backoff()
    )
    runtime.launch()
    return asyncio.run(_drive_load(app, revision_hash, configuration_hash, concurrency))


def test_concurrent_fake_executor_runs_name_the_first_observed_pressure(
    runtime: DbosRuntime,
) -> None:
    """Two in-process fake-executor runs name each door; a larger n is leftover.

    CI concurrency is two so this stays inside the cheap parallel suite. The
    operator command in OPERATIONS.md raises the same harness; it must still
    name the first observed pressure rather than dump a traceback.
    """

    concurrency = requested_concurrency()
    report = measure_load(runtime, concurrency)
    rendered = format_report(report)
    print(rendered)
    for sample in report.samples:
        assert NAMED_OUTCOME.fullmatch(sample.outcome), (
            f"door {sample.door.value} answered with {sample.outcome!r}; "
            f"a traceback is not a measurement\n{rendered}"
        )
        assert sample.cause in ObservedCause
    pressure = report.first_observed_pressure
    assert pressure.door in Door
    assert NAMED_OUTCOME.fullmatch(pressure.outcome)
    if concurrency == CI_CONCURRENCY:
        assert report.completed_runs == concurrency, rendered
        assert report.failed_runs == 0, rendered
