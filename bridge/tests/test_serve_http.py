"""Sidecar request handling: argument contract and the watchdog trip path.

The trip path is the reason these exist. It is the one behaviour that cannot be
exercised against a real Resolve on demand -- it needs a genuine modal stall --
so it is driven here with a command that simply sleeps. What is asserted is the
property the design promises: a trip latches, reports the CLI's timeout exit
code, and marks the process fatal so the abandoned call cannot land later.
"""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import resolve_cli  # noqa: E402
import serve_http  # noqa: E402


@pytest.fixture
def stub_command():
    """Register a fake command, then put COMMANDS back exactly as it was."""
    registered = []

    def register(name, fn):
        resolve_cli.COMMANDS[name] = fn
        registered.append(name)
        return name

    yield register
    for name in registered:
        del resolve_cli.COMMANDS[name]


def test_trip_latches_and_marks_fatal(stub_command):
    """A command that outlives its budget: 503, latch set, process marked fatal."""
    started = threading.Event()

    def sleeper(args, resolve):
        started.set()
        time.sleep(30)  # never returns within the budget below

    name = stub_command("_test-sleeper", sleeper)
    args = serve_http._build_args(name, {"timeout": serve_http.MIN_REQUEST_TIMEOUT_S})
    state = {"degraded": None}

    status, payload = serve_http._run_command(name, args, object(), state)

    assert started.is_set(), "the worker never ran, so this proved nothing"
    assert status == 503
    assert payload["exit_code"] == resolve_cli.EXIT_TIMEOUT
    assert state["degraded"], "the latch must be set so queued requests bail out"
    assert state["fatal"] is True, "a trip must stop the process, not just answer"


def test_completed_command_does_not_latch(stub_command):
    """The trip path must not fire for a command that simply finishes."""
    def quick(args, resolve):
        resolve_cli.set_output_sink({})  # handlers normally write through the sink
        return None

    name = stub_command("_test-quick", quick)
    args = serve_http._build_args(name, {})
    state = {"degraded": None}

    status, payload = serve_http._run_command(name, args, object(), state)

    assert status == 200
    assert payload["ok"] is True
    assert state["degraded"] is None
    assert "fatal" not in state


def test_timeout_floor_rejected():
    """`?timeout=0` made join() return instantly and faked a stall for everyone."""
    with pytest.raises(ValueError, match="at least"):
        serve_http._build_args("status", {"timeout": 0})


def test_timeout_at_floor_accepted():
    args = serve_http._build_args("status", {"timeout": serve_http.MIN_REQUEST_TIMEOUT_S})
    assert args.timeout == serve_http.MIN_REQUEST_TIMEOUT_S


@pytest.mark.parametrize("supplied,expected", [
    ({"audio": "true"}, True),
    ({"audio": "0"}, False),
    ({"audio": True}, True),
])
def test_bool_coercion(supplied, expected):
    assert serve_http._build_args("clips", supplied).audio is expected


def test_bool_rejects_nonsense():
    with pytest.raises(ValueError, match="boolean"):
        serve_http._build_args("clips", {"audio": "maybe"})


def test_numbers_stringify_for_text_fields():
    """A JSON caller sending {"fps": 29.97} means the string the CLI would get."""
    assert serve_http._build_args("build", {"fps": 29.97}).fps == "29.97"


def test_structural_values_rejected():
    """Previously reached a handler and died as a confusing exit-4 TypeError."""
    with pytest.raises(ValueError, match="string"):
        serve_http._build_args("render", {"name": {"not": "a name"}})


def test_defaults_shared_with_cli():
    """The sidecar must not invent its own idea of an unset flag."""
    args = serve_http._build_args("status", {})
    for field, default in resolve_cli.ARG_DEFAULTS.items():
        if field in ("json", "timeout"):
            continue  # sidecar forces json; timeout has its own floor
        assert getattr(args, field) == default
