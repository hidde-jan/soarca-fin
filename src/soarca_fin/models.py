"""Wire-protocol models for SOARCA's Fin protocol.

These mirror the JSON shapes defined in SOARCA's ``pkg/models/fin`` package
and the CACAO-derived ``capability.ResolvedTarget`` shape it reuses for
targets/authentication. They are deliberately permissive (``extra="allow"``)
where they carry CACAO data SOARCA itself treats as opaque passthrough, so a
newer/older SOARCA version can add fields without breaking this client.

Application code does not normally need to import from this module directly;
:mod:`soarca_fin.context` provides the ergonomic, framework-facing view of
the same data.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    """Base for all wire models: tolerant of unknown fields, uses field
    aliases so Python attributes stay ``snake_case`` while the wire format
    (already snake_case here, but kept explicit for clarity/consistency)
    matches SOARCA's Go JSON tags exactly."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Variable(_Model):
    """A CACAO variable (``pkg/models/cacao/variables.go``)."""

    type: str
    name: str | None = None
    description: str | None = None
    value: str | None = None
    constant: bool = False
    external: bool = False


Variables = dict[str, Variable]


class Target(_Model):
    """A CACAO agent-target (``cacao.AgentTarget``), as resolved by SOARCA -
    i.e. already looked up from the playbook's ``target_definitions``, not a
    ``target_definitions`` key."""

    id: str | None = None
    type: str | None = None
    name: str | None = None
    description: str | None = None
    address: dict[str, list[str]] | None = None
    port: str | None = None
    category: list[str] | None = None


class Authentication(_Model):
    """CACAO authentication information (``cacao.AuthenticationInformation``),
    already resolved by SOARCA from the playbook's
    ``authentication_info_definitions``."""

    id: str | None = None
    type: str | None = None
    username: str | None = None
    password: str | None = None
    token: str | None = None
    oauth_header: str | None = None
    private_key: str | None = None
    kms: bool = False
    kms_key_identifier: str | None = None


class ResolvedTarget(_Model):
    """One resolved target + its authentication - SOARCA's one canonical
    wire shape for this, shared across the Manual and Fin protocols
    (``capability.ResolvedTarget``)."""

    target: Target
    authentication: Authentication | None = None


class Command(_Model):
    """One command in a step's ``commands`` array."""

    type: str
    command: str | None = None
    command_b64: str | None = None
    content: str | None = None
    content_b64: str | None = None
    headers: dict[str, list[str]] | None = None


class StepInfo(_Model):
    """The subset of a CACAO step's own metadata forwarded with a job."""

    name: str | None = None
    description: str | None = None
    timeout: int | None = None
    delay: int | None = None


class JobState(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


class Job(_Model):
    """One poll-able unit of work: one CACAO step invocation."""

    job_id: UUID
    execution_id: UUID
    playbook_id: str
    step_id: str
    step_execution_id: UUID
    capability_type: str
    lease_expires_in_seconds: int
    step: StepInfo = Field(default_factory=StepInfo)
    commands: list[Command] = Field(default_factory=list)
    targets: list[ResolvedTarget] = Field(default_factory=list)
    variables: Variables = Field(default_factory=dict)


class TargetResult(_Model):
    """Optional, additive per-target diagnostic detail on a job result."""

    target_index: int | None = None
    state: JobState
    failed_command_index: int | None = None
    variables: Variables = Field(default_factory=dict)
    error: str | None = None


class Capability(_Model):
    """One capability type this Fin process declares at registration."""

    type: str
    description: str | None = None
    version: str | None = None
    step_examples: list[dict[str, Any]] | None = None


class RegisterRequest(_Model):
    registration_token: str
    display_name: str | None = None
    protocol_version: str | None = None
    capabilities: list[Capability]


class RegisterResponse(_Model):
    fin_id: str
    fin_token: str
    poll_interval_seconds: int
    long_poll_timeout_seconds: int
    job_lease_seconds: int


class PollRequest(_Model):
    concurrency_available: int | None = None


class PollResponse(_Model):
    job: Job


class ResultRequest(_Model):
    state: JobState
    variables: Variables = Field(default_factory=dict)
    error: str | None = None
    target_results: list[TargetResult] | None = None


class StatusPingRequest(_Model):
    progress: str | None = None


class StatusPingResponse(_Model):
    action: str | None = None


class FinRecord(_Model):
    """A registered Fin, as returned by the (admin-only) discovery
    endpoints. Not needed to implement a Fin, but included for
    completeness/tooling."""

    fin_id: str
    display_name: str | None = None
    protocol_version: str | None = None
    capabilities: list[Capability] = Field(default_factory=list)
    registered_at: datetime
    last_seen: datetime
