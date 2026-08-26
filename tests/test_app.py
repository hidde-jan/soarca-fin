"""Tests for the Fin application class: decorator registration, capability
derivation, and an end-to-end register -> poll -> execute -> submit flow
against a mocked SOARCA (via respx)."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest
import respx

from soarca_fin.app import Fin
from soarca_fin.context import CommandContext
from soarca_fin.credentials import InMemoryCredentialStore
from soarca_fin.exceptions import FinError

BASE_URL = "http://soarca.test"


def test_command_decorator_registers_handler_and_derives_capability() -> None:
    fin = Fin(BASE_URL, registration_token="secret")

    @fin.command("ssh-runner", description="Runs commands over SSH")
    async def handler(ctx: CommandContext) -> None:
        pass

    assert "ssh-runner" in fin._handlers
    capability = fin._handlers["ssh-runner"].to_capability()
    assert capability.type == "ssh-runner"
    assert capability.description == "Runs commands over SSH"


def test_registering_same_capability_type_twice_raises() -> None:
    fin = Fin(BASE_URL, registration_token="secret")

    @fin.command("ssh-runner")
    async def handler_one(ctx: CommandContext) -> None:
        pass

    with pytest.raises(FinError, match="already registered"):

        @fin.command("ssh-runner")
        async def handler_two(ctx: CommandContext) -> None:
            pass


async def test_run_async_without_handlers_raises() -> None:
    fin = Fin(BASE_URL, registration_token="secret")
    with pytest.raises(FinError, match="no capability handlers"):
        await fin.run_async()


@respx.mock
async def test_end_to_end_register_poll_execute_submit() -> None:
    fin = Fin(
        BASE_URL,
        registration_token="secret",
        credential_store=InMemoryCredentialStore(),
        concurrency=1,
    )

    received: list[str] = []

    @fin.command("ssh-runner")
    async def handler(ctx: CommandContext) -> dict[str, str]:
        received.append(ctx.command.command or "")
        return {"output": "done"}

    respx.post(f"{BASE_URL}/fin/register").mock(
        return_value=httpx.Response(
            201,
            json={
                "fin_id": "fin-1",
                "fin_token": "token-1",
                "poll_interval_seconds": 1,
                "long_poll_timeout_seconds": 1,
                "job_lease_seconds": 60,
            },
        )
    )

    job_id = uuid4()
    job_payload = {
        "job": {
            "job_id": str(job_id),
            "execution_id": str(uuid4()),
            "playbook_id": "playbook--1",
            "step_id": "step--1",
            "step_execution_id": str(uuid4()),
            "capability_type": "ssh-runner",
            "lease_expires_in_seconds": 60,
            "step": {},
            "commands": [{"type": "ssh-runner", "command": "ls"}],
            "targets": [],
            "variables": {},
        }
    }

    poll_calls = 0

    async def poll_handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_calls
        poll_calls += 1
        if poll_calls == 1:
            return httpx.Response(200, json=job_payload)
        # Real long-polling blocks server-side until a job appears or the
        # timeout elapses; simulate that here so the worker loop doesn't
        # busy-spin against an always-instant mock.
        await asyncio.sleep(0.02)
        return httpx.Response(204)

    respx.post(f"{BASE_URL}/fin/poll").mock(side_effect=poll_handler)

    submit_route = respx.put(f"{BASE_URL}/fin/jobs/{job_id}").mock(return_value=httpx.Response(204))

    async def stop_after_submit() -> None:
        # Poll a second time (204, no job) so the worker loop is idle, then
        # cancel it - this test only needs one job executed end-to-end.
        while not submit_route.called:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        raise asyncio.CancelledError

    task = asyncio.create_task(fin.run_async())
    stopper = asyncio.create_task(stop_after_submit())
    try:
        await stopper
    except asyncio.CancelledError:
        pass
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert received == ["ls"]
    assert submit_route.called
    sent_body = submit_route.calls.last.request.content
    assert b'"success"' in sent_body
    assert b'"output"' in sent_body
