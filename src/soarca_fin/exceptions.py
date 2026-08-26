"""Exceptions raised by soarca_fin.

Handlers only ever need :class:`FinJobError` (to fail a job/command with a
specific message); everything else here is raised by the framework itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class FinError(Exception):
    """Base class for all errors raised by this library."""


class FinJobError(FinError):
    """Raise from inside a handler to explicitly fail the current job (full
    step handlers) or the current command/target (per-command handlers)
    with a specific message, optionally still reporting variables gathered
    before the failure.

    Any other exception raised by a handler is treated the same way (its
    ``str()`` becomes the failure message) - this exists for cases where
    you want to control the message and/or attach variables explicitly.
    """

    def __init__(self, message: str, *, variables: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.variables: Mapping[str, Any] = variables or {}


class RegistrationError(FinError):
    """Raised when ``POST /fin/register`` fails (invalid/missing
    registration token, no capabilities registered, SOARCA unreachable,
    etc.)."""


class SoarcaApiError(FinError):
    """Raised for an unexpected (non-2xx, not otherwise handled) response
    from SOARCA."""

    def __init__(self, message: str, *, status_code: int, body: str = "") -> None:
        super().__init__(f"{message} (HTTP {status_code}): {body}")
        self.status_code = status_code
        self.body = body


class AuthenticationError(FinError):
    """Raised when SOARCA rejects this Fin's credential (``fin_token``) as
    invalid or unknown - e.g. because the Fin was unregistered/purged
    server-side. Stored credentials are unusable at this point; register()
    must be called again."""
