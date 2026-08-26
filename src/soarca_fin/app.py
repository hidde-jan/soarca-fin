"""The public framework surface: the ``Fin`` application object.

Typical usage - registration is a separate, explicit, one-time step, never
part of the normal run loop, so define the ``Fin`` in a module and drive it
via the ``soarca-fin`` CLI (see :mod:`soarca_fin.cli`) rather than calling
both ``register()`` and ``run()`` unconditionally from the same script::

    # fin.py
    from soarca_fin import Fin, CommandContext

    fin = Fin("http://localhost:8080")


    @fin.command("my-tool", description="Runs my-tool over SSH")
    async def run_my_tool(ctx: CommandContext) -> dict[str, str]:
        ...
        return {"output": "..."}

Then, from a shell (``--app`` is only needed if your module/variable aren't
named ``fin``/``app``; see :mod:`soarca_fin.cli`)::

    soarca-fin register --token my-registration-secret  # once
    soarca-fin run  # every subsequent run
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
from soarca_fin.exceptions import AuthenticationError, FinError
from soarca_fin.models import (
    Job,
    PollRequest,
    RegisterRequest,
    StatusPingRequest,
)
from soarca_fin.registration import FileRegistrationStore, FinRegistration, RegistrationStore
from soarca_fin.registry import CommandHandler, HandlerKind, HandlerSpec, StepHandler
from soarca_fin.runner import run_job

logger = logging.getLogger("soarca_fin")

_DEFAULT_STATUS_PING_FRACTION = 0.5
"""Send a status ping this fraction of the way through the job's lease, to
keep it comfortably alive without hammering the server."""

_DEFAULT_POLL_INTERVAL_SECONDS = 5
_DEFAULT_LONG_POLL_TIMEOUT_SECONDS = 30
_DEFAULT_JOB_LEASE_SECONDS = 60
"""Used only when running with an explicit fin_token and no operational
parameter override given (so the actual values SOARCA's /fin/register would
have returned are unknown). Override via run()/run_async() if these don't
match your SOARCA instance's configuration."""


class Fin:
    """A Fin process: a pool of capability handlers, registered with one
    SOARCA instance and run against it.

    :param base_url: SOARCA's base URL, e.g. ``"http://localhost:8080"``.
    :param display_name: Human-readable name shown in SOARCA's Fin
        discovery/admin views, used only when :meth:`register` is called.
    :param protocol_version: Sent as ``protocol_version`` at registration -
        the version of SOARCA's Fin protocol this process implements.
        Defaults to ``"1"``; override only if you're deliberately targeting
        a different protocol version.
    :param registration_store: Where :meth:`register` persists the
        :class:`~soarca_fin.registration.FinRegistration` it is issued
        (credentials plus server-chosen operational parameters), and where
        :meth:`run`/:meth:`run_async` read it back from - so a registration
        only ever needs to happen once, and restarting this process never
        re-registers a new identity. Defaults to a JSON file at
        ``~/.soarca-fin/registration.json``. Pass
        :class:`~soarca_fin.registration.InMemoryRegistrationStore` to opt
        out of persistence entirely (registration is then required on every
        restart).
    :param concurrency: How many jobs this Fin process handles at once.

    Registration is deliberately not automatic: call :meth:`register` (or
    :meth:`register_async`) yourself, once, typically from a setup step
    separate from your normal run - e.g. an operator-run CLI command,
    rather than something that happens implicitly every time the Fin
    process starts. :meth:`run`/:meth:`run_async` never register on your
    behalf and raise if no usable registration can be found.
    """

    def __init__(
        self,
        base_url: str,
        *,
        display_name: str | None = None,
        protocol_version: str = "1",
        registration_store: RegistrationStore | None = None,
        concurrency: int = 1,
    ) -> None:
        self.base_url = base_url
        self.display_name = display_name
        self.protocol_version = protocol_version
        self.registration_store = registration_store or FileRegistrationStore()
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
        return self._register_handler(
            capability_type, HandlerKind.STEP, description, version, examples
        )

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
        return self._register_handler(
            capability_type, HandlerKind.COMMAND, description, version, examples
        )

    def _register_handler(
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

    def register(self, registration_token: str) -> FinRegistration:
        """Blocking wrapper around :meth:`register_async` - see there for
        details."""
        return asyncio.run(self.register_async(registration_token))

    def build_register_request(self, registration_token: str) -> RegisterRequest:
        """Builds the exact :class:`~soarca_fin.models.RegisterRequest` body
        :meth:`register`/:meth:`register_async` would send to ``POST
        /fin/register`` - including the capabilities derived from your
        ``@fin.step``/``@fin.command`` handlers - without making any network
        call or touching ``registration_store``.

        Useful for debugging what registration would actually declare, e.g.::

            print(fin.build_register_request("token").model_dump_json(indent=2))

        or via the CLI: ``soarca-fin register --dry-run``.
        """
        if not self._handlers:
            raise FinError("no capability handlers registered - use @fin.step or @fin.command")

        return RegisterRequest(
            registration_token=registration_token,
            display_name=self.display_name,
            protocol_version=self.protocol_version,
            capabilities=[spec.to_capability() for spec in self._handlers.values()],
        )

    async def register_async(self, registration_token: str) -> FinRegistration:
        """Registers this Fin process with SOARCA using ``registration_token``
        (the shared secret configured server-side as
        ``FIN_REGISTRATION_TOKEN``), and persists the resulting
        :class:`~soarca_fin.registration.FinRegistration` via
        ``registration_store`` so :meth:`run`/:meth:`run_async` can find it
        afterwards.

        This is an explicit, one-time setup step - call it yourself (e.g.
        from a CLI flag or a separate setup script), not automatically on
        every run. ``registration_token`` itself is a one-time bootstrap
        secret and is **never persisted** - only the issued ``fin_id``/
        ``fin_token`` and operational parameters are stored.

        Calling this again mints a brand new, unrelated Fin identity and
        overwrites any previously stored registration.
        """
        request = self.build_register_request(registration_token)

        async with httpx.AsyncClient(base_url=self.base_url.rstrip("/")) as http_client:
            client = SoarcaClient(self.base_url, http_client=http_client)
            response = await client.register(request)

        registration = FinRegistration(
            fin_id=response.fin_id,
            fin_token=response.fin_token,
            poll_interval_seconds=response.poll_interval_seconds,
            long_poll_timeout_seconds=response.long_poll_timeout_seconds,
            job_lease_seconds=response.job_lease_seconds,
        )
        self.registration_store.save(registration)
        logger.info("registered with SOARCA as fin_id=%s", registration.fin_id)
        return registration

    def run(
        self,
        *,
        fin_token: str | None = None,
        poll_interval_seconds: int = _DEFAULT_POLL_INTERVAL_SECONDS,
        long_poll_timeout_seconds: int = _DEFAULT_LONG_POLL_TIMEOUT_SECONDS,
        job_lease_seconds: int = _DEFAULT_JOB_LEASE_SECONDS,
    ) -> None:
        """Blocking entry point: runs forever, until interrupted (e.g.
        Ctrl-C / SIGTERM). See :meth:`run_async` for parameter details."""
        asyncio.run(
            self.run_async(
                fin_token=fin_token,
                poll_interval_seconds=poll_interval_seconds,
                long_poll_timeout_seconds=long_poll_timeout_seconds,
                job_lease_seconds=job_lease_seconds,
            )
        )

    async def run_async(
        self,
        *,
        fin_token: str | None = None,
        poll_interval_seconds: int = _DEFAULT_POLL_INTERVAL_SECONDS,
        long_poll_timeout_seconds: int = _DEFAULT_LONG_POLL_TIMEOUT_SECONDS,
        job_lease_seconds: int = _DEFAULT_JOB_LEASE_SECONDS,
    ) -> None:
        """Async entry point, for callers who already manage their own
        event loop (e.g. embedding a Fin inside a larger asyncio
        application).

        Requires this Fin to already be registered: either a registration
        was previously persisted by :meth:`register`/:meth:`register_async`
        (the common case - loaded from ``registration_store``), or you pass
        ``fin_token`` explicitly here (e.g. one you manage in your own
        secrets store, obtained out-of-band). Raises
        :class:`~soarca_fin.exceptions.FinError` if neither is available -
        call :meth:`register` first.

        :param fin_token: Use this token directly instead of reading from
            ``registration_store``. Never persisted. Since the operational
            parameters SOARCA would normally hand back at registration
            aren't available in this mode, they default to conservative
            values below - override ``poll_interval_seconds``/
            ``long_poll_timeout_seconds``/``job_lease_seconds`` if you know
            SOARCA's actual configuration.
        """
        if not self._handlers:
            raise FinError("no capability handlers registered - use @fin.step or @fin.command")

        registration = self._resolve_registration(
            fin_token, poll_interval_seconds, long_poll_timeout_seconds, job_lease_seconds
        )

        async with httpx.AsyncClient(base_url=self.base_url.rstrip("/")) as http_client:
            client = SoarcaClient(self.base_url, http_client=http_client)

            workers = [
                asyncio.create_task(self._worker_loop(client, registration))
                for _ in range(self.concurrency)
            ]
            try:
                await asyncio.gather(*workers)
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

    def _resolve_registration(
        self,
        fin_token: str | None,
        poll_interval_seconds: int,
        long_poll_timeout_seconds: int,
        job_lease_seconds: int,
    ) -> FinRegistration:
        if fin_token:
            return FinRegistration(
                fin_id="",
                fin_token=fin_token,
                poll_interval_seconds=poll_interval_seconds,
                long_poll_timeout_seconds=long_poll_timeout_seconds,
                job_lease_seconds=job_lease_seconds,
            )

        existing = self.registration_store.load()
        if existing is not None:
            logger.info("using stored Fin registration (fin_id=%s)", existing.fin_id)
            return existing

        raise FinError(
            "not registered - call register()/register_async() first, or pass fin_token= "
            "explicitly to run()/run_async()"
        )

    def unregister(self, *, fin_id: str | None = None, fin_token: str | None = None) -> None:
        """Blocking wrapper around :meth:`unregister_async` - see there for
        details."""
        asyncio.run(self.unregister_async(fin_id=fin_id, fin_token=fin_token))

    async def unregister_async(
        self, *, fin_id: str | None = None, fin_token: str | None = None
    ) -> None:
        """Removes this Fin's registration from SOARCA (``DELETE
        /fin/{fin_id}``, authenticated with that Fin's own ``fin_token`` - a
        Fin may only unregister itself).

        If ``fin_id``/``fin_token`` aren't both given explicitly, they are
        read from ``registration_store`` (the same store :meth:`register`/
        :meth:`register_async` write to). Pass both explicitly to unregister
        a Fin identity that isn't the one currently stored locally (e.g. one
        you tracked yourself, or are cleaning up after losing the local
        store).

        Clears the stored registration afterwards, if it matches the
        ``fin_id`` just unregistered, so a later :meth:`run`/:meth:`run_async`
        correctly fails with "not registered" instead of retrying a token
        SOARCA no longer recognizes.
        """
        if (fin_id is None) != (fin_token is None):
            raise FinError(
                "pass both fin_id= and fin_token= explicitly, or neither "
                "(to use the stored registration)"
            )

        if fin_id is None or fin_token is None:
            existing = self.registration_store.load()
            if existing is None:
                raise FinError(
                    "not registered - nothing to unregister (or pass fin_id=/fin_token= explicitly)"
                )
            fin_id, fin_token = existing.fin_id, existing.fin_token

        async with httpx.AsyncClient(base_url=self.base_url.rstrip("/")) as http_client:
            client = SoarcaClient(self.base_url, http_client=http_client)
            await client.unregister(fin_token, fin_id)

        stored = self.registration_store.load()
        if stored is not None and stored.fin_id == fin_id:
            self.registration_store.clear()

        logger.info("unregistered fin_id=%s from SOARCA", fin_id)

    async def _worker_loop(self, client: SoarcaClient, registration: FinRegistration) -> None:
        while True:
            try:
                job = await self._poll_once(client, registration)
            except AuthenticationError:
                logger.error(
                    "SOARCA rejected our fin_token as invalid/unknown - clearing stored "
                    "registration; call register() again before restarting"
                )
                self.registration_store.clear()
                raise FinError(
                    "SOARCA rejected our fin_token - it is stale or unknown; re-register"
                ) from None
            except FinError as error:
                logger.warning("poll failed: %s", error)
                await asyncio.sleep(registration.poll_interval_seconds)
                continue

            if job is None:
                continue

            await self._execute_job(client, registration, job)

    async def _poll_once(self, client: SoarcaClient, registration: FinRegistration) -> Job | None:
        response = await client.poll(
            registration.fin_token,
            PollRequest(),
            long_poll_timeout_seconds=registration.long_poll_timeout_seconds,
        )
        return response.job if response else None

    async def _execute_job(
        self, client: SoarcaClient, registration: FinRegistration, job: Job
    ) -> None:
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
                registration.fin_token, job.job_id, StatusPingRequest(progress=text)
            )

        report_progress: ReportProgress = _report_progress

        keepalive_interval = job.lease_expires_in_seconds * _DEFAULT_STATUS_PING_FRACTION
        keepalive = asyncio.create_task(
            self._keep_lease_alive(client, registration, job, keepalive_interval)
        )
        try:
            result = await run_job(job, spec, report_progress)
        finally:
            keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keepalive

        try:
            await client.submit_result(registration.fin_token, job.job_id, result)
        except FinError as error:
            logger.error("failed to submit result for job %s: %s", job.job_id, error)

    async def _keep_lease_alive(
        self, client: SoarcaClient, registration: FinRegistration, job: Job, interval: float
    ) -> None:
        while True:
            await asyncio.sleep(interval)
            with contextlib.suppress(FinError):
                await client.status_ping(registration.fin_token, job.job_id, StatusPingRequest())
