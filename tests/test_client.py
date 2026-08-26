"""Tests for SoarcaClient against a mocked SOARCA HTTP API (via respx)."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
import respx

from soarca_fin.client import SoarcaClient
from soarca_fin.exceptions import AuthenticationError, RegistrationError, SoarcaApiError
from soarca_fin.models import (
    Capability,
    PollRequest,
    RegisterRequest,
    ResultRequest,
    StatusPingRequest,
)

BASE_URL = "http://soarca.test"


@respx.mock
async def test_register_success() -> None:
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
    async with SoarcaClient(BASE_URL) as client:
        response = await client.register(
            RegisterRequest(registration_token="secret", capabilities=[Capability(type="ssh")])
        )

    assert response.fin_id == "fin-1"
    assert response.fin_token == "token-1"


@respx.mock
async def test_register_failure_raises_registration_error() -> None:
    respx.post(f"{BASE_URL}/fin/register").mock(
        return_value=httpx.Response(403, json={"message": "invalid registration token"})
    )
    async with SoarcaClient(BASE_URL) as client:
        with pytest.raises(RegistrationError, match="invalid registration token"):
            await client.register(
                RegisterRequest(registration_token="bad", capabilities=[Capability(type="ssh")])
            )


@respx.mock
async def test_poll_no_job_returns_none() -> None:
    respx.post(f"{BASE_URL}/fin/poll").mock(return_value=httpx.Response(204))
    async with SoarcaClient(BASE_URL) as client:
        result = await client.poll("token-1", PollRequest(), long_poll_timeout_seconds=1)

    assert result is None


@respx.mock
async def test_poll_returns_job() -> None:
    job_id = str(uuid4())
    execution_id = str(uuid4())
    step_execution_id = str(uuid4())
    respx.post(f"{BASE_URL}/fin/poll").mock(
        return_value=httpx.Response(
            200,
            json={
                "job": {
                    "job_id": job_id,
                    "execution_id": execution_id,
                    "playbook_id": "playbook--1",
                    "step_id": "step--1",
                    "step_execution_id": step_execution_id,
                    "capability_type": "ssh",
                    "lease_expires_in_seconds": 60,
                    "step": {},
                    "commands": [{"type": "ssh", "command": "ls"}],
                    "targets": [],
                    "variables": {},
                }
            },
        )
    )
    async with SoarcaClient(BASE_URL) as client:
        result = await client.poll("token-1", PollRequest(), long_poll_timeout_seconds=1)

    assert result is not None
    assert str(result.job.job_id) == job_id
    assert result.job.commands[0].command == "ls"


@respx.mock
async def test_poll_rejects_bad_token() -> None:
    respx.post(f"{BASE_URL}/fin/poll").mock(
        return_value=httpx.Response(401, json={"message": "unknown fin_token"})
    )
    async with SoarcaClient(BASE_URL) as client:
        with pytest.raises(AuthenticationError):
            await client.poll("bad-token", PollRequest(), long_poll_timeout_seconds=1)


@respx.mock
async def test_submit_result_success() -> None:
    job_id = uuid4()
    route = respx.put(f"{BASE_URL}/fin/jobs/{job_id}").mock(return_value=httpx.Response(204))
    async with SoarcaClient(BASE_URL) as client:
        await client.submit_result("token-1", job_id, ResultRequest(state="success"))

    assert route.called
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer token-1"


@respx.mock
async def test_submit_result_failure_raises_api_error() -> None:
    job_id = uuid4()
    respx.put(f"{BASE_URL}/fin/jobs/{job_id}").mock(return_value=httpx.Response(500, text="boom"))
    async with SoarcaClient(BASE_URL) as client:
        with pytest.raises(SoarcaApiError):
            await client.submit_result("token-1", job_id, ResultRequest(state="failure"))


@respx.mock
async def test_status_ping() -> None:
    job_id = uuid4()
    respx.patch(f"{BASE_URL}/fin/jobs/{job_id}/status").mock(
        return_value=httpx.Response(200, json={"action": "continue"})
    )
    async with SoarcaClient(BASE_URL) as client:
        response = await client.status_ping("token-1", job_id, StatusPingRequest())

    assert response.action == "continue"


@respx.mock
async def test_unregister() -> None:
    route = respx.delete(f"{BASE_URL}/fin/fin-1").mock(return_value=httpx.Response(204))
    async with SoarcaClient(BASE_URL) as client:
        await client.unregister("token-1", "fin-1")

    assert route.called
