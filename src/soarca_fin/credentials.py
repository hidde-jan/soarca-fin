"""Persisting Fin registration credentials across restarts.

SOARCA's Fin registrations are database-backed precisely so a Fin process
does not need to re-register every time it restarts (see
``docs/adr/FIN-WEBHOOK-PROTOCOL-PROPOSAL.md`` in the SOARCA repository) -
but only if the Fin process itself remembers the ``fin_id``/``fin_token`` it
was issued. :class:`CredentialStore` is that memory.
"""

from __future__ import annotations

import json
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol


@dataclass(slots=True, frozen=True)
class Credentials:
    """What a successful registration hands back, persisted so a restarted
    Fin process can start polling immediately instead of registering again
    (which would mint a brand new, unrelated ``fin_id``)."""

    fin_id: str
    fin_token: str
    poll_interval_seconds: int
    long_poll_timeout_seconds: int
    job_lease_seconds: int


class CredentialStore(Protocol):
    """Storage for :class:`Credentials`. Implement this to plug in your own
    backend (a secrets manager, a database row, ...) instead of the default
    file-based store."""

    def load(self) -> Credentials | None:
        """Return the previously-saved credentials, or ``None`` if none are
        stored yet (a fresh registration is then required)."""

    def save(self, credentials: Credentials) -> None:
        """Persist credentials for future runs."""

    def clear(self) -> None:
        """Discard stored credentials (e.g. after the server rejects the
        token as unknown, or the Fin explicitly unregisters)."""


class FileCredentialStore:
    """Default :class:`CredentialStore`: a single JSON file, created with
    owner-only permissions (0600) since it contains a bearer credential."""

    def __init__(self, path: str | Path = "~/.soarca-fin/credentials.json") -> None:
        self.path = Path(path).expanduser()

    def load(self) -> Credentials | None:
        if not self.path.exists():
            return None
        data = json.loads(self.path.read_text())
        return Credentials(**data)

    def save(self, credentials: Credentials) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(credentials), indent=2))
        self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class InMemoryCredentialStore:
    """A :class:`CredentialStore` that only lives for the process's
    lifetime - every restart registers a new Fin identity. Useful for tests
    and short-lived/ephemeral Fin processes (e.g. one-shot jobs, CI
    runners) where persistence across restarts is not wanted."""

    def __init__(self) -> None:
        self._credentials: Credentials | None = None

    def load(self) -> Credentials | None:
        return self._credentials

    def save(self, credentials: Credentials) -> None:
        self._credentials = credentials

    def clear(self) -> None:
        self._credentials = None
