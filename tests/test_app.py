"""Tests for the Fin application class: decorator registration, capability
derivation, explicit registration, and end-to-end run loops against a
mocked SOARCA (via respx)."""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import astuple
from uuid import uuid4

import httpx
import pytest
import respx

from soarca_fin.app import Fin
from soarca_fin.client import SoarcaClient
from soarca_fin.context import CommandContext
from soarca_fin.exceptions import FinError
from soarca_fin.registration import FinRegistration, InMemoryRegistrationStore

BASE_URL = "http://soarca.test"


def _job_payload(capability_type: str = "ssh-runner") -> tuple[object, dict[str, object]]:
    job_id = uuid4()
    payload = {
        "job": {
            "job_id": str(job_id),
            "execution_id": str(uuid4()),
            "playbook_id": "playbook--1",
            "step_id": "step--1",
            "step_execution_id": str(uuid4()),
            "capability_type": capability_type,
            "lease_expires_in_seconds": 60,
            "step": {},
            "commands": [{"type": capability_type, "command": "ls"}],
            "targets": [],
            "variables": {},
        }
    }
    return job_id, payload


def test_command_decorator_registers_handler_and_derives_capability() -> None:
    fin = Fin(BASE_URL)

    @fin.command("ssh-runner", description="Runs commands over SSH")
    async def handler(ctx: CommandContext) -> None:
        pass

    assert "ssh-runner" in fin._handlers
    capability = fin._handlers["ssh-runner"].to_capability()
    assert capability.type == "ssh-runner"
    assert capability.description == "Runs commands over SSH"


def test_registering_same_capability_type_twice_raises() -> None:
    fin = Fin(BASE_URL)

    @fin.command("ssh-runner")
    async def handler_one(ctx: CommandContext) -> None:
        pass

    with pytest.raises(FinError, match="already registered"):

        @fin.command("ssh-runner")
        async def handler_two(ctx: CommandContext) -> None:
            pass


async def test_run_async_without_handlers_raises() -> None:
    fin = Fin(BASE_URL)
    with pytest.raises(FinError, match="no capability handlers"):
        await fin.run_async()


async def test_register_async_without_handlers_raises() -> None:
    fin = Fin(BASE_URL)
    with pytest.raises(FinError, match="no capability handlers"):
        await fin.register_async("secret")


def test_build_register_request_without_handlers_raises() -> None:
    fin = Fin(BASE_URL)
    with pytest.raises(FinError, match="no capability handlers"):
        fin.build_register_request("secret")


def test_build_register_request_reflects_handlers_without_network_or_storage() -> None:
    store = InMemoryRegistrationStore()
    fin = Fin(BASE_URL, display_name="my-fin", registration_store=store)

    @fin.command("ssh-runner", description="Runs commands over SSH")
    async def handler(ctx: CommandContext) -> None:
        pass

    request = fin.build_register_request("my-registration-secret")

    assert request.registration_token == "my-registration-secret"
    assert request.display_name == "my-fin"
    assert request.protocol_version == "1"
    assert [c.type for c in request.capabilities] == ["ssh-runner"]
    assert request.capabilities[0].description == "Runs commands over SSH"
    # Purely local - no registration should have been attempted or stored.
    assert store.load() is None


def test_protocol_version_is_overridable() -> None:
    fin = Fin(BASE_URL, protocol_version="2")

    @fin.command("ssh-runner")
    async def handler(ctx: CommandContext) -> None:
        pass

    assert fin.build_register_request("secret").protocol_version == "2"


async def test_run_async_without_registration_or_fin_token_raises() -> None:
    fin = Fin(BASE_URL, registration_store=InMemoryRegistrationStore())

    @fin.command("ssh-runner")
    async def handler(ctx: CommandContext) -> None:
        pass

    with pytest.raises(FinError, match="not registered"):
        await fin.run_async()


@respx.mock
async def test_register_async_persists_registration_but_not_registration_token() -> None:
    store = InMemoryRegistrationStore()
    fin = Fin(BASE_URL, registration_store=store)

    @fin.command("ssh-runner")
    async def handler(ctx: CommandContext) -> None:
        pass

    respx.post(f"{BASE_URL}/fin/register").mock(
        return_value=httpx.Response(
            201,
            json={
                "fin_id": "fin-1",
                "fin_token": "token-1",
                "poll_interval_seconds": 5,
                "long_poll_timeout_seconds": 30,
                "job_lease_seconds": 60,
            },
        )
    )

    registration = await fin.register_async("my-registration-secret")

    assert registration.fin_id == "fin-1"
    assert registration.fin_token == "token-1"
    stored = store.load()
    assert stored == registration
    # The registration_token itself must never end up anywhere in the
    # persisted registration record.
    assert stored is not None
    assert "my-registration-secret" not in astuple(stored)


@respx.mock
async def test_run_async_uses_stored_registration_from_prior_registration() -> None:
    store = InMemoryRegistrationStore()
    store.save(
        FinRegistration(
            fin_id="fin-1",
            fin_token="token-1",
            poll_interval_seconds=1,
            long_poll_timeout_seconds=1,
            job_lease_seconds=60,
        )
    )
    fin = Fin(BASE_URL, registration_store=store, concurrency=1)

    received: list[str] = []

    @fin.command("ssh-runner")
    async def handler(ctx: CommandContext) -> dict[str, str]:
        received.append(ctx.command.command or "")
        return {"output": "done"}

    register_route = respx.post(f"{BASE_URL}/fin/register")
    job_id, job_payload = _job_payload()

    poll_calls = 0

    async def poll_handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_calls
        poll_calls += 1
        if poll_calls == 1:
            assert request.headers["authorization"] == "Bearer token-1"
            return httpx.Response(200, json=job_payload)
        await asyncio.sleep(0.02)
        return httpx.Response(204)

    respx.post(f"{BASE_URL}/fin/poll").mock(side_effect=poll_handler)
    submit_route = respx.put(f"{BASE_URL}/fin/jobs/{job_id}").mock(return_value=httpx.Response(204))

    await _run_until_submitted(fin, submit_route)

    assert not register_route.called
    assert received == ["ls"]
    sent_body = submit_route.calls.last.request.content
    assert b'"success"' in sent_body
    assert b'"output"' in sent_body


@respx.mock
async def test_run_async_with_explicit_fin_token_skips_registration_store() -> None:
    fin = Fin(BASE_URL, registration_store=InMemoryRegistrationStore(), concurrency=1)

    @fin.command("ssh-runner")
    async def handler(ctx: CommandContext) -> dict[str, str]:
        return {"output": "done"}

    register_route = respx.post(f"{BASE_URL}/fin/register")
    job_id, job_payload = _job_payload()

    poll_calls = 0

    async def poll_handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_calls
        poll_calls += 1
        if poll_calls == 1:
            assert request.headers["authorization"] == "Bearer already-known-token"
            return httpx.Response(200, json=job_payload)
        await asyncio.sleep(0.02)
        return httpx.Response(204)

    respx.post(f"{BASE_URL}/fin/poll").mock(side_effect=poll_handler)
    submit_route = respx.put(f"{BASE_URL}/fin/jobs/{job_id}").mock(return_value=httpx.Response(204))

    await _run_until_submitted(fin, submit_route, fin_token="already-known-token")

    assert not register_route.called
    assert submit_route.called
    assert fin.registration_store.load() is None  # explicit fin_token is never persisted


@respx.mock
async def test_run_async_rejected_token_clears_store_and_raises() -> None:
    store = InMemoryRegistrationStore()
    store.save(
        FinRegistration(
            fin_id="fin-1",
            fin_token="stale-token",
            poll_interval_seconds=1,
            long_poll_timeout_seconds=1,
            job_lease_seconds=60,
        )
    )
    fin = Fin(BASE_URL, registration_store=store, concurrency=1)

    @fin.command("ssh-runner")
    async def handler(ctx: CommandContext) -> None:
        pass

    respx.post(f"{BASE_URL}/fin/poll").mock(
        return_value=httpx.Response(401, json={"message": "unknown fin_token"})
    )

    with pytest.raises(FinError, match="rejected"):
        await fin.run_async()

    assert store.load() is None


@respx.mock
async def test_unregister_async_uses_stored_registration_and_clears_store() -> None:
    store = InMemoryRegistrationStore()
    store.save(
        FinRegistration(
            fin_id="fin-1",
            fin_token="token-1",
            poll_interval_seconds=5,
            long_poll_timeout_seconds=30,
            job_lease_seconds=60,
        )
    )
    fin = Fin(BASE_URL, registration_store=store)

    route = respx.delete(f"{BASE_URL}/fin/").mock(return_value=httpx.Response(204))

    await fin.unregister_async()

    assert route.called
    assert route.calls.last.request.headers["Authorization"] == "Bearer token-1"
    assert store.load() is None


@respx.mock
async def test_unregister_async_with_explicit_token_leaves_unrelated_store_alone() -> None:
    store = InMemoryRegistrationStore()
    store.save(
        FinRegistration(
            fin_id="fin-1",
            fin_token="token-1",
            poll_interval_seconds=5,
            long_poll_timeout_seconds=30,
            job_lease_seconds=60,
        )
    )
    fin = Fin(BASE_URL, registration_store=store)

    route = respx.delete(f"{BASE_URL}/fin/").mock(return_value=httpx.Response(204))

    await fin.unregister_async(fin_token="token-2")

    assert route.called
    # fin-1/token-1 is a different, unrelated registration - it must not be
    # wiped out just because *some* unregister call happened.
    assert store.load() is not None
    assert store.load().fin_id == "fin-1"  # type: ignore[union-attr]


@respx.mock
async def test_unregister_async_explicit_token_matching_store_clears_it() -> None:
    store = InMemoryRegistrationStore()
    store.save(
        FinRegistration(
            fin_id="fin-1",
            fin_token="token-1",
            poll_interval_seconds=5,
            long_poll_timeout_seconds=30,
            job_lease_seconds=60,
        )
    )
    fin = Fin(BASE_URL, registration_store=store)

    respx.delete(f"{BASE_URL}/fin/").mock(return_value=httpx.Response(204))

    await fin.unregister_async(fin_token="token-1")

    assert store.load() is None


async def test_unregister_async_without_registration_or_args_raises() -> None:
    fin = Fin(BASE_URL, registration_store=InMemoryRegistrationStore())
    with pytest.raises(FinError, match="not registered"):
        await fin.unregister_async()


@respx.mock
async def test_unregister_async_propagates_api_error() -> None:
    from soarca_fin.exceptions import SoarcaApiError

    store = InMemoryRegistrationStore()
    store.save(
        FinRegistration(
            fin_id="fin-1",
            fin_token="token-1",
            poll_interval_seconds=5,
            long_poll_timeout_seconds=30,
            job_lease_seconds=60,
        )
    )
    fin = Fin(BASE_URL, registration_store=store)

    respx.delete(f"{BASE_URL}/fin/").mock(
        return_value=httpx.Response(500, json={"message": "boom"})
    )

    with pytest.raises(SoarcaApiError):
        await fin.unregister_async()

    # A failed unregister must not clear a still-potentially-valid store.
    assert store.load() is not None


async def _run_until_submitted(
    fin: Fin, submit_route: respx.Route, *, fin_token: str | None = None
) -> None:
    async def stop_after_submit() -> None:
        while not submit_route.called:
            await asyncio.sleep(0.01)
        raise asyncio.CancelledError

    task = asyncio.create_task(fin.run_async(fin_token=fin_token))
    stopper = asyncio.create_task(stop_after_submit())
    try:
        await stopper
    except asyncio.CancelledError:
        pass
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------


def _registration(**overrides: object) -> FinRegistration:
    defaults: dict[str, object] = {
        "fin_id": "fin-1",
        "fin_token": "token-1",
        "poll_interval_seconds": 1,
        "long_poll_timeout_seconds": 30,
        "job_lease_seconds": 60,
    }
    defaults.update(overrides)
    return FinRegistration(**defaults)  # type: ignore[arg-type]


@respx.mock
async def test_worker_loop_stops_polling_promptly_once_shutdown_requested() -> None:
    """An idle worker (no job claimed yet) must not wait out the rest of a
    long-poll once a graceful shutdown is requested - it should give up on
    that poll immediately."""
    fin = Fin(BASE_URL, registration_store=InMemoryRegistrationStore(), concurrency=1)

    @fin.command("ssh-runner")
    async def handler(ctx: CommandContext) -> dict[str, str]:
        return {}

    poll_calls = 0

    async def poll_handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_calls
        poll_calls += 1
        await asyncio.sleep(10)  # much longer than the test's own timeout below
        return httpx.Response(204)

    respx.post(f"{BASE_URL}/fin/poll").mock(side_effect=poll_handler)

    registration = _registration()
    shutdown_event = asyncio.Event()
    async with httpx.AsyncClient(base_url=BASE_URL) as http_client:
        client = SoarcaClient(BASE_URL, http_client=http_client)
        worker = asyncio.create_task(fin._worker_loop(client, registration, shutdown_event))
        await asyncio.sleep(0.02)  # let the worker start its long-poll
        shutdown_event.set()
        await asyncio.wait_for(worker, timeout=1)

    assert poll_calls == 1


@respx.mock
async def test_worker_loop_finishes_in_flight_job_despite_shutdown_request() -> None:
    """A job already claimed must run to completion (and submit its
    result) even if a graceful shutdown was requested while it was
    running - it is never abandoned."""
    fin = Fin(BASE_URL, registration_store=InMemoryRegistrationStore(), concurrency=1)

    handler_started = asyncio.Event()
    finish_handler = asyncio.Event()

    @fin.command("ssh-runner")
    async def handler(ctx: CommandContext) -> dict[str, str]:
        handler_started.set()
        await finish_handler.wait()
        return {"output": "done"}

    job_id, job_payload = _job_payload()
    respx.post(f"{BASE_URL}/fin/poll").mock(return_value=httpx.Response(200, json=job_payload))
    respx.patch(f"{BASE_URL}/fin/jobs/{job_id}/status").mock(
        return_value=httpx.Response(200, json={})
    )
    submit_route = respx.put(f"{BASE_URL}/fin/jobs/{job_id}").mock(return_value=httpx.Response(204))

    registration = _registration()
    shutdown_event = asyncio.Event()
    async with httpx.AsyncClient(base_url=BASE_URL) as http_client:
        client = SoarcaClient(BASE_URL, http_client=http_client)
        worker = asyncio.create_task(fin._worker_loop(client, registration, shutdown_event))
        await asyncio.wait_for(handler_started.wait(), timeout=1)

        shutdown_event.set()  # graceful shutdown requested mid-job
        await asyncio.sleep(0.02)
        assert not submit_route.called, "in-flight job must not be abandoned"

        finish_handler.set()
        await asyncio.wait_for(worker, timeout=1)

    assert submit_route.called


@respx.mock
async def test_run_async_shutdown_event_finishes_in_flight_job_then_exits() -> None:
    """End-to-end via run_async: setting shutdown_event triggers a graceful
    shutdown - the in-flight job still completes and run_async returns
    normally afterwards (no new job is polled for)."""
    fin = Fin(BASE_URL, registration_store=InMemoryRegistrationStore(), concurrency=1)

    handler_started = asyncio.Event()
    finish_handler = asyncio.Event()

    @fin.command("ssh-runner")
    async def handler(ctx: CommandContext) -> dict[str, str]:
        handler_started.set()
        await finish_handler.wait()
        return {"output": "done"}

    job_id, job_payload = _job_payload()
    respx.post(f"{BASE_URL}/fin/poll").mock(return_value=httpx.Response(200, json=job_payload))
    respx.patch(f"{BASE_URL}/fin/jobs/{job_id}/status").mock(
        return_value=httpx.Response(200, json={})
    )
    submit_route = respx.put(f"{BASE_URL}/fin/jobs/{job_id}").mock(return_value=httpx.Response(204))

    shutdown_event = asyncio.Event()
    run_task = asyncio.create_task(fin.run_async(fin_token="tok", shutdown_event=shutdown_event))
    await asyncio.wait_for(handler_started.wait(), timeout=1)

    shutdown_event.set()
    await asyncio.sleep(0.02)
    assert not submit_route.called

    finish_handler.set()
    await asyncio.wait_for(run_task, timeout=1)  # returns normally, no exception

    assert submit_route.called


@respx.mock
async def test_run_async_cancellation_forces_immediate_shutdown() -> None:
    """Cancelling the task running run_async (the caller's own way of
    forcing an immediate shutdown, e.g. after a second signal in an
    embedding application) abandons any in-flight job instead of waiting
    for a handler that never finishes on its own."""
    fin = Fin(BASE_URL, registration_store=InMemoryRegistrationStore(), concurrency=1)

    handler_started = asyncio.Event()

    @fin.command("ssh-runner")
    async def handler(ctx: CommandContext) -> dict[str, str]:
        handler_started.set()
        await asyncio.sleep(100)  # never finishes on its own
        return {"output": "done"}

    job_id, job_payload = _job_payload()
    respx.post(f"{BASE_URL}/fin/poll").mock(return_value=httpx.Response(200, json=job_payload))
    respx.patch(f"{BASE_URL}/fin/jobs/{job_id}/status").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.put(f"{BASE_URL}/fin/jobs/{job_id}").mock(return_value=httpx.Response(204))

    shutdown_event = asyncio.Event()
    run_task = asyncio.create_task(fin.run_async(fin_token="tok", shutdown_event=shutdown_event))
    await asyncio.wait_for(handler_started.wait(), timeout=1)

    shutdown_event.set()
    await asyncio.sleep(0.02)
    run_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run_task, timeout=1)


@respx.mock
async def test_run_async_shutdown_grace_period_forces_cancellation_of_hung_job() -> None:
    """shutdown_grace_period_seconds forces a shutdown on its own, without
    needing an explicit cancellation, once it elapses with the job still
    running."""
    fin = Fin(BASE_URL, registration_store=InMemoryRegistrationStore(), concurrency=1)

    handler_started = asyncio.Event()

    @fin.command("ssh-runner")
    async def handler(ctx: CommandContext) -> dict[str, str]:
        handler_started.set()
        await asyncio.sleep(100)  # never finishes on its own
        return {"output": "done"}

    job_id, job_payload = _job_payload()
    respx.post(f"{BASE_URL}/fin/poll").mock(return_value=httpx.Response(200, json=job_payload))
    respx.patch(f"{BASE_URL}/fin/jobs/{job_id}/status").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.put(f"{BASE_URL}/fin/jobs/{job_id}").mock(return_value=httpx.Response(204))

    shutdown_event = asyncio.Event()
    run_task = asyncio.create_task(
        fin.run_async(
            fin_token="tok", shutdown_event=shutdown_event, shutdown_grace_period_seconds=0.05
        )
    )
    await asyncio.wait_for(handler_started.wait(), timeout=1)

    shutdown_event.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run_task, timeout=1)


@respx.mock
async def test_run_with_default_signal_handling_sigterm_finishes_in_flight_job() -> None:
    """End-to-end: a real SIGTERM triggers a graceful shutdown via the
    default signal handling installed by run()/the CLI - the in-flight
    job still completes and the run returns normally afterwards (no new
    job is polled for)."""
    fin = Fin(BASE_URL, registration_store=InMemoryRegistrationStore(), concurrency=1)

    handler_started = asyncio.Event()
    finish_handler = asyncio.Event()

    @fin.command("ssh-runner")
    async def handler(ctx: CommandContext) -> dict[str, str]:
        handler_started.set()
        await finish_handler.wait()
        return {"output": "done"}

    job_id, job_payload = _job_payload()
    respx.post(f"{BASE_URL}/fin/poll").mock(return_value=httpx.Response(200, json=job_payload))
    respx.patch(f"{BASE_URL}/fin/jobs/{job_id}/status").mock(
        return_value=httpx.Response(200, json={})
    )
    submit_route = respx.put(f"{BASE_URL}/fin/jobs/{job_id}").mock(return_value=httpx.Response(204))

    run_task = asyncio.create_task(fin._run_with_default_signal_handling(fin_token="tok"))
    await asyncio.wait_for(handler_started.wait(), timeout=1)

    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.sleep(0.02)
    assert not submit_route.called

    finish_handler.set()
    await asyncio.wait_for(run_task, timeout=1)  # returns normally, no exception

    assert submit_route.called


@respx.mock
async def test_run_with_default_signal_handling_second_sigterm_forces_immediate_shutdown() -> None:
    """A second SIGTERM (sent after a graceful shutdown is already under
    way) abandons any in-flight job and forces an immediate exit, instead
    of waiting for a handler that never finishes on its own."""
    fin = Fin(BASE_URL, registration_store=InMemoryRegistrationStore(), concurrency=1)

    handler_started = asyncio.Event()

    @fin.command("ssh-runner")
    async def handler(ctx: CommandContext) -> dict[str, str]:
        handler_started.set()
        await asyncio.sleep(100)  # never finishes on its own
        return {"output": "done"}

    job_id, job_payload = _job_payload()
    respx.post(f"{BASE_URL}/fin/poll").mock(return_value=httpx.Response(200, json=job_payload))
    respx.patch(f"{BASE_URL}/fin/jobs/{job_id}/status").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.put(f"{BASE_URL}/fin/jobs/{job_id}").mock(return_value=httpx.Response(204))

    run_task = asyncio.create_task(fin._run_with_default_signal_handling(fin_token="tok"))
    await asyncio.wait_for(handler_started.wait(), timeout=1)

    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.sleep(0.02)
    os.kill(os.getpid(), signal.SIGTERM)

    # _run_with_default_signal_handling suppresses the CancelledError from
    # the forced shutdown itself - there's no caller left to propagate a
    # cancellation to, this is the process's own top-level entry point.
    await asyncio.wait_for(run_task, timeout=1)
