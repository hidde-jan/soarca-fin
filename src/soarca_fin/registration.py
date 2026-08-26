"""Persisting a Fin's registration record across restarts.

SOARCA's Fin registrations are database-backed precisely so a Fin process
does not need to re-register every time it restarts (see
``docs/adr/FIN-WEBHOOK-PROTOCOL-PROPOSAL.md`` in the SOARCA repository) -
but only if the Fin process itself remembers what registration handed back.
:class:`RegistrationStore` is that memory.

A successful registration produces two distinct kinds of information, both
bundled into :class:`FinRegistration`:

- **credentials**: ``fin_id``/``fin_token``, used to authenticate later
  requests.
- **operational parameters**: ``poll_interval_seconds``,
  ``long_poll_timeout_seconds``, ``job_lease_seconds`` - server-chosen
  timing values, not secrets, but likewise worth persisting so a restarted
  Fin process doesn't need to guess or re-register just to learn them.

Neither on its own is "the credentials", hence the name.
"""

from __future__ import annotations

import json
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol


@dataclass(slots=True, frozen=True)
class FinRegistration:
    """What a successful registration hands back, persisted so a restarted
    Fin process can start polling immediately instead of registering again
    (which would mint a brand new, unrelated ``fin_id``)."""

    fin_id: str
    fin_token: str
    poll_interval_seconds: int
    long_poll_timeout_seconds: int
    job_lease_seconds: int


class RegistrationStore(Protocol):
    """Storage for a :class:`FinRegistration`. Implement this to plug in
    your own backend (a secrets manager, a database row, ...) instead of
    the default file-based store."""

    def load(self) -> FinRegistration | None:
        """Return the previously-saved registration, or ``None`` if none is
        stored yet (a fresh registration is then required)."""

    def save(self, registration: FinRegistration) -> None:
        """Persist a registration for future runs."""

    def clear(self) -> None:
        """Discard the stored registration (e.g. after the server rejects
        the token as unknown, or the Fin explicitly unregisters)."""


class FileRegistrationStore:
    """Default :class:`RegistrationStore`: a single JSON file, created with
    owner-only permissions (0600) since it contains a bearer credential."""

    def __init__(self, path: str | Path = "~/.soarca-fin/registration.json") -> None:
        self.path = Path(path).expanduser()

    def load(self) -> FinRegistration | None:
        if not self.path.exists():
            return None
        data = json.loads(self.path.read_text())
        return FinRegistration(**data)

    def save(self, registration: FinRegistration) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(registration), indent=2))
        self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class InMemoryRegistrationStore:
    """A :class:`RegistrationStore` that only lives for the process's
    lifetime - every restart registers a new Fin identity. Useful for tests
    and short-lived/ephemeral Fin processes (e.g. one-shot jobs, CI
    runners) where persistence across restarts is not wanted."""

    def __init__(self) -> None:
        self._registration: FinRegistration | None = None

    def load(self) -> FinRegistration | None:
        return self._registration

    def save(self, registration: FinRegistration) -> None:
        self._registration = registration

    def clear(self) -> None:
        self._registration = None
