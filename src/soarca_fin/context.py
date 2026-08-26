"""The developer-facing view of a job - what handlers actually receive.

Handlers never see :mod:`soarca_fin.models` wire types directly for the
job/step metadata; they get :class:`JobMeta` plus either a full
:class:`StepContext` or a per-invocation :class:`CommandContext`, with
timeouts already converted to :class:`~datetime.timedelta` and no lease/poll
plumbing in sight.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

from soarca_fin.models import Command, ResolvedTarget, Variable

handler_logger = logging.getLogger("soarca_fin.handler")
"""Logger namespace for handler code, via ``ctx.log`` - kept separate from
``soarca_fin``'s own lifecycle/framework logging so you can configure
verbosity for your handlers independently of the library's internals."""


class _ContextLoggerAdapter(logging.LoggerAdapter[logging.Logger]):
    """Prefixes every message with job-identifying context (e.g.
    ``job_id``, ``target_index``), so log lines are traceable back to a
    specific job/command even with the standard logging format."""

    def process(self, msg: object, kwargs: Any) -> tuple[object, Any]:
        if self.extra:
            context = " ".join(f"{key}={value}" for key, value in self.extra.items())
            msg = f"[{context}] {msg}"
        return msg, kwargs


def _bind_logger(**extra: object) -> logging.LoggerAdapter[logging.Logger]:
    """Build a :class:`logging.LoggerAdapter` that automatically prefixes
    every record with job-identifying fields, so log lines from concurrent
    jobs (or concurrent commands/targets within one job) can be told apart
    without every handler having to format that context by hand."""
    return _ContextLoggerAdapter(handler_logger, extra)


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
    log: logging.LoggerAdapter[logging.Logger]
    """Logger pre-bound with this job's identifying context (job id,
    execution id, step id, capability type). Log through this instead of
    grabbing your own logger, so your handler's log lines are
    automatically traceable back to the job that produced them."""


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
    log: logging.LoggerAdapter[logging.Logger]
    """Logger pre-bound with this job's identifying context plus
    ``target_index``/``command_index``, so log lines from concurrent
    commands/targets within the same job are still individually
    traceable."""
