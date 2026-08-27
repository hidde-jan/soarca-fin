"""Thin async HTTP client for SOARCA's Fin protocol endpoints.

This module deals in wire models (:mod:`soarca_fin.models`) and raw
HTTP/JSON. Nothing here is meant to be used directly by Fin authors - see
:class:`soarca_fin.app.Fin` for the framework surface.
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Self
from uuid import UUID

import httpx

from soarca_fin.exceptions import AuthenticationError, RegistrationError, SoarcaApiError
from soarca_fin.models import (
    PollRequest,
    PollResponse,
    RegisterRequest,
    RegisterResponse,
    ResultRequest,
    StatusPingRequest,
    StatusPingResponse,
)

logger = logging.getLogger("soarca_fin")

# A little slack added on top of long_poll_timeout_seconds for the HTTP
# client's own read timeout, so SOARCA's own timeout response always wins
# the race over the client timing out the connection first.
_POLL_TIMEOUT_SLACK_SECONDS = 5.0

# httpx/httpcore keep pooled connections around for reuse across requests.
# If the peer (SOARCA, a proxy, the OS) closes an idle pooled connection at
# just the wrong moment, httpx can start writing the next request onto it
# before noticing it's dead, raising RemoteProtocolError ("Server
# disconnected without sending a response") instead of transparently
# reconnecting. Since that failure mode means the request was never seen by
# the server, retrying once with a fresh connection is always safe here.
_TRANSIENT_CONNECTION_ERRORS = (httpx.RemoteProtocolError, httpx.ConnectError)


class SoarcaClient:
    """Talks to one SOARCA instance's Fin protocol endpoints
    (``/fin/register``, ``/fin/poll``, ``/fin/jobs/{id}``,
    ``/fin/jobs/{id}/status``, ``/fin/{id}``)."""

    def __init__(self, base_url: str, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(base_url=base_url.rstrip("/"))
        if http_client is not None:
            # Respect a caller-supplied client's own base_url if it has one;
            # otherwise anchor it to base_url so relative paths below work.
            self._http.base_url = httpx.URL(str(self._http.base_url) or base_url.rstrip("/"))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    async def register(self, request: RegisterRequest) -> RegisterResponse:
        response = await self._request_with_retry(
            "POST", "/fin/register", json=request.model_dump(mode="json")
        )
        if response.status_code == 201:
            return RegisterResponse.model_validate(response.json())
        raise RegistrationError(_error_message(response))

    async def poll(
        self, fin_token: str, request: PollRequest, *, long_poll_timeout_seconds: float
    ) -> PollResponse | None:
        """Long-polls for the next job. Returns ``None`` if none became
        available before the timeout - this is the expected, common case,
        not an error."""
        response = await self._authenticated_request(
            "POST",
            "/fin/poll",
            fin_token,
            json=request.model_dump(mode="json", exclude_none=True),
            timeout=httpx.Timeout(
                long_poll_timeout_seconds + _POLL_TIMEOUT_SLACK_SECONDS,
                connect=10.0,
            ),
        )
        if response.status_code == 204:
            return None
        if response.status_code == 200:
            return PollResponse.model_validate(response.json())
        raise SoarcaApiError("poll failed", status_code=response.status_code, body=response.text)

    async def submit_result(self, fin_token: str, job_id: UUID, request: ResultRequest) -> None:
        response = await self._authenticated_request(
            "PUT",
            f"/fin/jobs/{job_id}",
            fin_token,
            json=request.model_dump(mode="json", exclude_none=True),
        )
        if response.status_code != 204:
            raise SoarcaApiError(
                "submitting job result failed", status_code=response.status_code, body=response.text
            )

    async def status_ping(
        self, fin_token: str, job_id: UUID, request: StatusPingRequest
    ) -> StatusPingResponse:
        response = await self._authenticated_request(
            "PATCH",
            f"/fin/jobs/{job_id}/status",
            fin_token,
            json=request.model_dump(mode="json", exclude_none=True),
        )
        if response.status_code == 200:
            return StatusPingResponse.model_validate(response.json())
        raise SoarcaApiError(
            "extending job lease failed", status_code=response.status_code, body=response.text
        )

    async def unregister(self, fin_token: str) -> None:
        """Unregisters the calling Fin itself. The fin_id is inferred
        server-side from fin_token, so it is never sent explicitly - a Fin
        can only ever unregister itself, never another one."""
        response = await self._authenticated_request("DELETE", "/fin/", fin_token)
        if response.status_code != 204:
            raise SoarcaApiError(
                "unregistering failed", status_code=response.status_code, body=response.text
            )

    async def _authenticated_request(
        self, method: str, path: str, fin_token: str, **kwargs: object
    ) -> httpx.Response:
        response = await self._request_with_retry(
            method,
            path,
            headers={"Authorization": f"Bearer {fin_token}"},
            **kwargs,
        )
        if response.status_code == 401:
            raise AuthenticationError(_error_message(response))
        return response

    async def _request_with_retry(
        self, method: str, path: str, **kwargs: object
    ) -> httpx.Response:
        try:
            return await self._http.request(method, path, **kwargs)  # type: ignore[arg-type]
        except _TRANSIENT_CONNECTION_ERRORS as exc:
            logger.warning(
                "transient connection error on %s %s (%s), retrying once",
                method,
                path,
                exc,
            )
            return await self._http.request(method, path, **kwargs)  # type: ignore[arg-type]


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
        message = body.get("message") if isinstance(body, dict) else None
    except ValueError:
        message = None
    return message or response.text or f"HTTP {response.status_code}"
