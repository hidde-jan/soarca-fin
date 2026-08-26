"""The developer-facing view of a job - what handlers actually receive.

Handlers never see :mod:`soarca_fin.models` wire types directly for the
job/step metadata; they get :class:`JobMeta` plus either a full
:class:`StepContext` or a per-invocation :class:`CommandContext`, with
timeouts already converted to :class:`~datetime.timedelta` and no lease/poll
plumbing in sight.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

from soarca_fin.models import Command, ResolvedTarget, Variable


@dataclass(slots=True, frozen=True)
class JobMeta:
    """Identifying/descriptive metadata about the step invocation a job
    belongs to - the same for every command/target within one job."""

    execution_id: UUID
    playbook_id: str
    step_id: str
    step_execution_id: UUID
    capability_type: str
    name: str | None
    description: str | None
    timeout: timedelta | None
    delay: timedelta | None
    variables: Mapping[str, Variable] = field(default_factory=dict)


ReportProgress = Callable[[str], Awaitable[None]]


@dataclass(slots=True, frozen=True)
class StepContext:
    """Passed to handlers registered with :meth:`soarca_fin.app.Fin.step`:
    the whole job at once, exactly as SOARCA sent it - your handler owns
    iterating over ``targets``/``commands`` and aggregating a result
    itself. Use this when you need full control (e.g. one connection
    reused across commands, or genuinely parallel target execution)."""

    job: JobMeta
    commands: list[Command]
    targets: list[ResolvedTarget]
    report_progress: ReportProgress
    """Optionally report human-readable progress for very long-running
    jobs. Calling this is never required for correctness - the lease is
    kept alive automatically in the background regardless."""


@dataclass(slots=True, frozen=True)
class CommandContext:
    """Passed to handlers registered with
    :meth:`soarca_fin.app.Fin.command`: one command against one target (or
    ``target=None`` if the step declared no targets at all)."""

    job: JobMeta
    command: Command
    target: ResolvedTarget | None
    target_index: int | None
    """Position of ``target`` within the job's ``targets`` list, or ``None``
    when the step declared no targets. Included so results can be
    correlated back to a specific target in ``target_results``."""
    report_progress: ReportProgress
