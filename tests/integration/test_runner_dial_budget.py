"""One dial to Core spends one budget, across the connect and the handshake both.

`socket.create_connection` and the TLS handshake `wrap_socket` performs wait on
the same socket timeout, so a dial that hands each of them the whole remaining
budget can take twice it -- and the pair is what an invocation's span pays for
while a provider child is held. `dial_within_budget` turns the budget into one
deadline and gives the handshake only what the connect left.

The arithmetic is what these proofs pin, so the clock is scripted rather than
read: two readings, handed out in order, with a third read failing loudly. Real
sockets and a real CA carry the rest, because what the handshake was actually
given is only observable on a real one.
"""

from __future__ import annotations

import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from atelier2.adapters.runner_tls import CORE_DNS_NAME
from atelier2.runner.__main__ import dial_within_budget
from tests.integration.test_runner_session_wire import (
    _client_tls_context,
    _loopback_authority,
    _LoopbackAuthority,
    _runner_identity,
)

# Large enough that a dial which wrongly hands it to the handshake as well is
# unmistakable against the sliver the scripted connect leaves behind, and far
# beyond anything a loaded host needs for a loopback handshake.
_BUDGET_SECONDS = 30.0
_SLIVER_SECONDS = 0.1

# The only wall-clock element in this file, and only ever a ceiling: a dial
# that took the whole budget rather than the sliver, or a handshake thread that
# never finished, must fail here instead of hanging the suite.
_DEADLOCK_CEILING_SECONDS = 10.0

_RUNNER_URI = "urn:atelier2:runner-dial-budget:v1"


@dataclass
class _ScriptedClock:
    """A clock whose readings this test hands out, one at a time and in order.

    The dial's budget is arithmetic over exactly two readings -- one before the
    connect and one after it -- so scripting those two is what makes "the
    handshake got the remainder" a fact rather than a stopwatch reading. A
    third reading exhausts the script and fails loudly, which pins that the
    dial reads its clock twice and never again.
    """

    readings: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.readings.pop(0)

    def sleep(self, seconds: float) -> None:
        del seconds
        raise AssertionError("dialling Core never sleeps")


@dataclass(frozen=True, slots=True)
class _SilentCore:
    """A listener that answers TCP and never a word of TLS."""

    listener: socket.socket

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.listener.getsockname()[:2]
        return host, port


def _silent_core() -> _SilentCore:
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(_DEADLOCK_CEILING_SECONDS)
    return _SilentCore(listener)


def _runner_context(tmp_path: Path, authority: _LoopbackAuthority) -> ssl.SSLContext:
    certificate, key, _leaf = _runner_identity(tmp_path, authority, _RUNNER_URI)
    context = _client_tls_context(authority, certificate, key)
    # The same fence production sets: the peer leaf is pinned afterwards
    # against the bootstrap document, never by the built-in hostname check.
    context.check_hostname = False
    return context


def test_a_dial_gives_the_handshake_only_what_the_connect_left(
    tmp_path: Path,
) -> None:
    """The residual this file exists for.

    The scripted clock says the connect spent all but a sliver of the budget.
    Core answers TCP and then says nothing, so the handshake can only end by
    running out of time -- and it must run out of the sliver, not of a second
    helping of the whole budget.
    """
    authority = _loopback_authority(tmp_path)
    core = _silent_core()
    clock = _ScriptedClock([0.0, _BUDGET_SECONDS - _SLIVER_SECONDS])

    with core.listener:
        started = time.monotonic()
        with pytest.raises(OSError):
            dial_within_budget(
                core.address,
                _runner_context(tmp_path, authority),
                CORE_DNS_NAME,
                _BUDGET_SECONDS,
                clock,
            )
        elapsed = time.monotonic() - started

    assert elapsed < _DEADLOCK_CEILING_SECONDS
    assert clock.readings == []


def test_a_connect_that_spends_the_whole_budget_never_starts_a_handshake(
    tmp_path: Path,
) -> None:
    """Nothing is attempted on a budget that is already gone.

    The scripted clock says the connect used every second of it. Core's own
    accepted socket is then read to the end: a handshake that had been started
    would have put a ClientHello there, so reaching end-of-stream having read
    nothing at all is what proves none was.
    """
    authority = _loopback_authority(tmp_path)
    core = _silent_core()
    clock = _ScriptedClock([0.0, _BUDGET_SECONDS])

    with core.listener:
        with pytest.raises(TimeoutError):
            dial_within_budget(
                core.address,
                _runner_context(tmp_path, authority),
                CORE_DNS_NAME,
                _BUDGET_SECONDS,
                clock,
            )
        accepted, _peer = core.listener.accept()
        with accepted:
            accepted.settimeout(_DEADLOCK_CEILING_SECONDS)
            said = accepted.recv(1)

    assert said == b""
    assert clock.readings == []


def test_a_dial_inside_its_budget_reaches_an_authenticated_core(
    tmp_path: Path,
) -> None:
    """The budget still buys what it is for: one authenticated channel.

    The scripted clock says the connect cost nothing, so the whole budget
    remains for a handshake a real Core really completes.
    """
    authority = _loopback_authority(tmp_path)
    core = _silent_core()
    clock = _ScriptedClock([0.0, 0.0])
    served: list[bytes | None] = []

    def _serve() -> None:
        accepted, _peer = core.listener.accept()
        with authority.core_context.wrap_socket(accepted, server_side=True) as tls:
            served.append(tls.getpeercert(binary_form=True))

    with core.listener:
        handshaking = threading.Thread(target=_serve)
        handshaking.start()
        try:
            connection = dial_within_budget(
                core.address,
                _runner_context(tmp_path, authority),
                CORE_DNS_NAME,
                _BUDGET_SECONDS,
                clock,
            )
            with connection:
                presented = connection.getpeercert(binary_form=True)
        finally:
            handshaking.join(timeout=_DEADLOCK_CEILING_SECONDS)

    assert presented is not None
    assert served and served[0] is not None
    assert clock.readings == []
