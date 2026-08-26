"""Turns one polled :class:`~soarca_fin.models.Job` into a
:class:`~soarca_fin.models.ResultRequest`, by dispatching to whichever
handler was registered for the job's ``capability_type``.

The per-command/target aggregation policy implemented here
(:func:`_run_command_handler`) intentionally mirrors SOARCA's own built-in
SSH capability (``pkg/core/capability/ssh/ssh.go``): for each target, run
commands in order and stop at the first failure *for that target only*,
then continue on to the next target; merge reported variables across every
attempt (last write wins per name, via plain dict update, matching
``cacao.Variables.Merge``); the job's overall outcome is a failure if *any*
target/command failed, and the reported error message is whichever failure
was seen last.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from soarca_fin.context import CommandContext, JobMeta, ReportProgress, StepContext, _bind_logger
from soarca_fin.exceptions import FinJobError
from soarca_fin.models import Job, JobState, ResultRequest, TargetResult, Variable
from soarca_fin.registry import HandlerKind, HandlerSpec, call_handler

logger = logging.getLogger("soarca_fin")


@dataclass(slots=True, frozen=True)
class StepResult:
    """Optional richer return value for ``@fin.step`` handlers that want to
    report per-target diagnostics. Returning a plain ``dict`` (or
    ``None``) of variables is enough for the common case; only reach for
    this when you specifically want ``target_results`` populated."""

    variables: Mapping[str, Any] | None = None
    target_results: list[TargetResult] | None = None


def _as_variables(value: Mapping[str, Any] | None) -> dict[str, Variable]:
    if not value:
        return {}
    result: dict[str, Variable] = {}
    for name, raw in value.items():
        result[name] = raw if isinstance(raw, Variable) else Variable(type="string", value=str(raw))
    return result


def _job_meta(job: Job) -> JobMeta:
    return JobMeta(
        execution_id=job.execution_id,
        playbook_id=job.playbook_id,
        step_id=job.step_id,
        step_execution_id=job.step_execution_id,
        capability_type=job.capability_type,
        name=job.step.name,
        description=job.step.description,
        timeout=timedelta(seconds=job.step.timeout) if job.step.timeout is not None else None,
        delay=timedelta(seconds=job.step.delay) if job.step.delay is not None else None,
        variables=job.variables,
    )


async def run_job(job: Job, spec: HandlerSpec, report_progress: ReportProgress) -> ResultRequest:
    meta = _job_meta(job)
    handler_log = _bind_logger(
        job_id=str(job.job_id),
        execution_id=str(job.execution_id),
        step_id=job.step_id,
        capability_type=job.capability_type,
    )
    if spec.kind is HandlerKind.STEP:
        return await _run_step_handler(job, meta, spec, report_progress, handler_log)
    return await _run_command_handler(job, meta, spec, report_progress, handler_log)


async def _run_step_handler(
    job: Job,
    meta: JobMeta,
    spec: HandlerSpec,
    report_progress: ReportProgress,
    handler_log: logging.LoggerAdapter[logging.Logger],
) -> ResultRequest:
    context = StepContext(
        job=meta,
        commands=job.commands,
        targets=job.targets,
        report_progress=report_progress,
        log=handler_log,
    )
    logger.debug(
        "job %s: invoking step handler with %d command(s), %d target(s)",
        job.job_id,
        len(job.commands),
        len(job.targets),
    )
    try:
        outcome = await call_handler(spec.func, context)
    except FinJobError as error:
        logger.debug("job %s: step handler raised FinJobError: %s", job.job_id, error)
        return ResultRequest(
            state=JobState.FAILURE, variables=_as_variables(error.variables), error=str(error)
        )
    except Exception as error:  # noqa: BLE001 - any handler exception fails the job
        logger.debug("job %s: step handler raised %s: %s", job.job_id, type(error).__name__, error)
        return ResultRequest(state=JobState.FAILURE, variables={}, error=str(error))

    if isinstance(outcome, StepResult):
        return ResultRequest(
            state=JobState.SUCCESS,
            variables=_as_variables(outcome.variables),
            target_results=outcome.target_results,
        )
    return ResultRequest(state=JobState.SUCCESS, variables=_as_variables(outcome))


async def _run_command_handler(
    job: Job,
    meta: JobMeta,
    spec: HandlerSpec,
    report_progress: ReportProgress,
    handler_log: logging.LoggerAdapter[logging.Logger],
) -> ResultRequest:
    merged_variables: dict[str, Variable] = {}
    target_results: list[TargetResult] = []
    last_error: str | None = None

    targets: list[Any] = list(enumerate(job.targets)) if job.targets else [(None, None)]

    for target_index, target in targets:
        target_state = JobState.SUCCESS
        target_error: str | None = None
        failed_command_index: int | None = None

        for command_index, command in enumerate(job.commands):
            command_log = _bind_logger(
                **dict(handler_log.extra or {}),
                target_index=target_index,
                command_index=command_index,
            )
            context = CommandContext(
                job=meta,
                command=command,
                target=target,
                target_index=target_index,
                report_progress=report_progress,
                log=command_log,
            )
            logger.debug(
                "job %s: invoking command handler for target_index=%s command_index=%d (type=%s)",
                job.job_id,
                target_index,
                command_index,
                command.type,
            )
            try:
                outcome = await call_handler(spec.func, context)
            except FinJobError as error:
                logger.debug(
                    "job %s: command handler raised FinJobError for target_index=%s "
                    "command_index=%d: %s",
                    job.job_id,
                    target_index,
                    command_index,
                    error,
                )
                merged_variables.update(_as_variables(error.variables))
                target_state = JobState.FAILURE
                target_error = str(error)
                failed_command_index = command_index
                last_error = target_error
                break
            except Exception as error:  # noqa: BLE001 - any handler exception fails this target
                logger.debug(
                    "job %s: command handler raised %s for target_index=%s command_index=%d: %s",
                    job.job_id,
                    type(error).__name__,
                    target_index,
                    command_index,
                    error,
                )
                target_state = JobState.FAILURE
                target_error = str(error)
                failed_command_index = command_index
                last_error = target_error
                break
            else:
                merged_variables.update(_as_variables(outcome))

        target_results.append(
            TargetResult(
                target_index=target_index,
                state=target_state,
                failed_command_index=failed_command_index,
                variables={},
                error=target_error,
            )
        )

    overall_state = JobState.FAILURE if last_error is not None else JobState.SUCCESS
    return ResultRequest(
        state=overall_state,
        variables=merged_variables,
        error=last_error,
        target_results=target_results if job.targets else None,
    )
