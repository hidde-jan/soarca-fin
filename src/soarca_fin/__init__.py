"""soarca_fin: a Flask-like library for implementing SOARCA Fins in Python.

Quick start::

    from soarca_fin import Fin, CommandContext

    fin = Fin("http://localhost:8080", registration_token="secret")


    @fin.command("my-tool")
    async def run_my_tool(ctx: CommandContext) -> dict[str, str]:
        ...
        return {"output": "..."}


    fin.run()
"""

from soarca_fin.app import Fin
from soarca_fin.context import CommandContext, JobMeta, StepContext
from soarca_fin.credentials import (
    Credentials,
    CredentialStore,
    FileCredentialStore,
    InMemoryCredentialStore,
)
from soarca_fin.exceptions import (
    AuthenticationError,
    FinError,
    FinJobError,
    RegistrationError,
    SoarcaApiError,
)
from soarca_fin.runner import StepResult

__all__ = [
    "AuthenticationError",
    "CommandContext",
    "Credentials",
    "CredentialStore",
    "Fin",
    "FileCredentialStore",
    "FinError",
    "FinJobError",
    "InMemoryCredentialStore",
    "JobMeta",
    "RegistrationError",
    "SoarcaApiError",
    "StepContext",
    "StepResult",
]
