"""soarca_fin: a Flask-like library for implementing SOARCA Fins in Python.

Quick start - save this as ``my_fin.py``::

    from soarca_fin import Fin, CommandContext

    fin = Fin("http://localhost:8080")


    @fin.command("my-tool")
    async def run_my_tool(ctx: CommandContext) -> dict[str, str]:
        ...
        return {"output": "..."}

Then drive it with the ``soarca-fin`` CLI, similar to ``flask --app``::

    soarca-fin --app my_fin:fin register --token my-registration-secret  # once
    soarca-fin --app my_fin:fin run  # every subsequent run
"""

from soarca_fin.app import Fin
from soarca_fin.context import CommandContext, JobMeta, StepContext
from soarca_fin.exceptions import (
    AuthenticationError,
    FinError,
    FinJobError,
    RegistrationError,
    SoarcaApiError,
)
from soarca_fin.registration import (
    FileRegistrationStore,
    FinRegistration,
    InMemoryRegistrationStore,
    RegistrationStore,
)
from soarca_fin.runner import StepResult

__all__ = [
    "AuthenticationError",
    "CommandContext",
    "Fin",
    "FileRegistrationStore",
    "FinError",
    "FinJobError",
    "FinRegistration",
    "InMemoryRegistrationStore",
    "JobMeta",
    "RegistrationError",
    "RegistrationStore",
    "SoarcaApiError",
    "StepContext",
    "StepResult",
]
