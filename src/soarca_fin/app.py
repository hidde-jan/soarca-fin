"""The public framework surface: the ``Fin`` application object.

Typical usage::

    from soarca_fin import Fin, CommandContext

    fin = Fin("http://localhost:8080", registration_token="secret")


    @fin.command("my-tool", description="Runs my-tool over SSH")
    async def run_my_tool(ctx: CommandContext) -> dict[str, str]:
        ...
        return {"output": "..."}


    fin.run()
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from soarca_fin.client import SoarcaClient
from soarca_fin.context import ReportProgress
from soarca_fin.credentials import Credentials, CredentialStore, FileCredentialStore
from soarca_fin.exceptions import AuthenticationError, FinError
from soarca_fin.models import (
    Job,
    PollRequest,
    RegisterRequest,
    StatusPingRequest,
)
from soarca_fin.registry import CommandHandler, HandlerKind, HandlerSpec, StepHandler
from soarca_fin.runner import run_job

logger = logging.getLogger("soarca_fin")

_DEFAULT_STATUS_PING_FRACTION = 0.5
"""Send a status ping this fraction of the way through the job's lease, to
keep it comfortably alive without hammering the server."""


class Fin:
    """A Fin process: a pool of capability handlers, registered with one
    SOARCA instance and run against it.

    :param base_url: SOARCA's base URL, e.g. ``"http://localhost:8080"``.
    :param registration_token: The shared secret SOARCA requires for
        ``POST /fin/register`` (``FIN_REGISTRATION_TOKEN`` server-side).
        Only needed the first time this Fin process registers; a
        previously stored ``fin_token`` (see ``credential_store``) is
        reused afterwards without it.
    :param display_name: Human-readable name shown in SOARCA's Fin
        discovery/admin views. Defaults to the process's own best guess
        (``sys.argv[0]``) if not given.
    :param credential_store: Where to persist the ``fin_id``/``fin_token``
        issued at registration, so restarting this process doesn't
        re-register a brand new identity. Defaults to a JSON file at
        ``~/.soarca-fin/credentials.json``. Pass
        :class:`~soarca_fin.credentials.InMemoryCredentialStore` to opt out
        of persistence entirely.
    :param concurrency: How many jobs this Fin process handles at once.
    """

    def __init__(
        self,
        base_url: str,
        *,
        registration_token: str | None = None,
        display_name: str | None = None,
        credential_store: CredentialStore | None = None,
        concurrency: int = 1,
    ) -> None:
        self.base_url = base_url
        self.registration_token = registration_token
        self.display_name = display_name
        self.credential_store = credential_store or FileCredentialStore()
        self.concurrency = concurrency
        self._handlers: dict[str, HandlerSpec] = {}

    def step(
        self,
        capability_type: str,
        *,
        description: str | None = None,
        version: str | None = None,
        examples: list[Mapping[str, Any]] | None = None,
    ) -> Callable[[StepHandler], StepHandler]:
        """Registers a handler that receives an entire step's job at once
        (all commands and all targets together) and is responsible for
        iterating/aggregating a result itself.

        The handler receives one :class:`~soarca_fin.context.StepContext`
        argument and may be ``async def`` or a plain ``def``. Its return
        value becomes the job's reported variables: return a ``dict`` (or
        ``None``) for the common case, or a
        :class:`~soarca_fin.runner.StepResult` for full control including
        per-target diagnostics. Raise
        :class:`~soarca_fin.exceptions.FinJobError` (or let any other
        exception propagate) to fail the job.
        """
        return self._register(capability_type, HandlerKind.STEP, description, version, examples)

    def command(
        self,
        capability_type: str,
        *,
        description: str | None = None,
        version: str | None = None,
        examples: list[Mapping[str, Any]] | None = None,
    ) -> Callable[[CommandHandler], CommandHandler]:
        """Registers a handler that is called once per (command, target)
        pair - the simplest way to implement a Fin when there is nothing
        step-specific to do.

        The handler receives one
        :class:`~soarca_fin.context.CommandContext` argument (``target`` is
        ``None`` if the step declared no targets) and may be ``async def``
        or a plain ``def``. Its return value becomes that command's
        reported variables (``dict`` or ``None``). Raise
        :class:`~soarca_fin.exceptions.FinJobError` (or let any other
        exception propagate) to fail that target's remaining commands;
        other targets still run. The job overall fails if any target
        failed, matching SOARCA's built-in capabilities' behaviour.
        """
        return self._register(capability_type, HandlerKind.COMMAND, description, version, examples)

    def _register(
        self,
        capability_type: str,
        kind: HandlerKind,
        description: str | None,
        version: str | None,
        examples: list[Mapping[str, Any]] | None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        if capability_type in self._handlers:
            raise FinError(
                f"a handler for capability type {capability_type!r} is already registered"
            )

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self._handlers[capability_type] = HandlerSpec(
                capability_type=capability_type,
                kind=kind,
                func=func,
                description=description,
                version=version,
                examples=examples,
            )
            return func

        return decorator

    def run(self) -> None:
        """Blocking entry point: registers (if needed) and runs forever,
        until interrupted (e.g. Ctrl-C / SIGTERM)."""
        asyncio.run(self.run_async())

    async def run_async(self) -> None:
        """Async entry point, for callers who already manage their own
        event loop (e.g. embedding a Fin inside a larger asyncio
        application)."""
        if not self._handlers:
            raise FinError("no capability handlers registered - use @fin.step or @fin.command")

        async with httpx.AsyncClient(base_url=self.base_url.rstrip("/")) as http_client:
            client = SoarcaClient(self.base_url, http_client=http_client)
            credentials = await self._ensure_registered(client)

            workers = [
                asyncio.create_task(self._worker_loop(client, credentials))
                for _ in range(self.concurrency)
            ]
            try:
                await asyncio.gather(*workers)
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

    async def _ensure_registered(self, client: SoarcaClient) -> Credentials:
        existing = self.credential_store.load()
        if existing is not None:
            logger.info("reusing stored Fin credentials (fin_id=%s)", existing.fin_id)
            return existing

        if not self.registration_token:
            raise FinError(
                "no stored credentials and no registration_token provided - "
                "cannot register with SOARCA"
            )

        request = RegisterRequest(
            registration_token=self.registration_token,
            display_name=self.display_name,
            capabilities=[spec.to_capability() for spec in self._handlers.values()],
        )
        response = await client.register(request)
        credentials = Credentials(
            fin_id=response.fin_id,
            fin_token=response.fin_token,
            poll_interval_seconds=response.poll_interval_seconds,
            long_poll_timeout_seconds=response.long_poll_timeout_seconds,
            job_lease_seconds=response.job_lease_seconds,
        )
        self.credential_store.save(credentials)
        logger.info("registered with SOARCA as fin_id=%s", credentials.fin_id)
        return credentials

    async def _worker_loop(self, client: SoarcaClient, credentials: Credentials) -> None:
        while True:
            try:
                job = await self._poll_once(client, credentials)
            except AuthenticationError:
                logger.warning("SOARCA rejected our fin_token - re-registering")
                self.credential_store.clear()
                credentials = await self._ensure_registered(client)
                continue
            except FinError as error:
                logger.warning("poll failed: %s", error)
                await asyncio.sleep(credentials.poll_interval_seconds)
                continue

            if job is None:
                continue

            await self._execute_job(client, credentials, job)

    async def _poll_once(self, client: SoarcaClient, credentials: Credentials) -> Job | None:
        response = await client.poll(
            credentials.fin_token,
            PollRequest(),
            long_poll_timeout_seconds=credentials.long_poll_timeout_seconds,
        )
        return response.job if response else None

    async def _execute_job(self, client: SoarcaClient, credentials: Credentials, job: Job) -> None:
        spec = self._handlers.get(job.capability_type)
        if spec is None:
            logger.error(
                "received job for unregistered capability type %r (job_id=%s)",
                job.capability_type,
                job.job_id,
            )
            return

        async def _report_progress(text: str) -> None:
            await client.status_ping(
                credentials.fin_token, job.job_id, StatusPingRequest(progress=text)
            )

        report_progress: ReportProgress = _report_progress

        keepalive_interval = job.lease_expires_in_seconds * _DEFAULT_STATUS_PING_FRACTION
        keepalive = asyncio.create_task(
            self._keep_lease_alive(client, credentials, job, keepalive_interval)
        )
        try:
            result = await run_job(job, spec, report_progress)
        finally:
            keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keepalive

        try:
            await client.submit_result(credentials.fin_token, job.job_id, result)
        except FinError as error:
            logger.error("failed to submit result for job %s: %s", job.job_id, error)

    async def _keep_lease_alive(
        self, client: SoarcaClient, credentials: Credentials, job: Job, interval: float
    ) -> None:
        while True:
            await asyncio.sleep(interval)
            with contextlib.suppress(FinError):
                await client.status_ping(credentials.fin_token, job.job_id, StatusPingRequest())
