"""Tests for the registry-to-ResultRequest aggregation logic in
soarca_fin.runner, covering the SSH-style per-target/per-command
semantics and the simpler full-step handler path."""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from soarca_fin.context import CommandContext, StepContext
from soarca_fin.exceptions import FinJobError
from soarca_fin.models import Command, Job, JobState, ResolvedTarget, StepInfo, Target
from soarca_fin.registry import HandlerKind, HandlerSpec
from soarca_fin.runner import StepResult, run_job


def _make_job(*, commands: list[Command], targets: list[ResolvedTarget]) -> Job:
    return Job(
        job_id=uuid4(),
        execution_id=uuid4(),
        playbook_id="playbook--1",
        step_id="step--1",
        step_execution_id=uuid4(),
        capability_type="test-tool",
        lease_expires_in_seconds=60,
        step=StepInfo(name="a step"),
        commands=commands,
        targets=targets,
    )


def _target(name: str) -> ResolvedTarget:
    return ResolvedTarget(target=Target(id=name, name=name))


async def _noop_progress(_: str) -> None:
    return None


async def test_command_handler_runs_once_per_command_target_pair() -> None:
    calls: list[tuple[str, str | None]] = []

    async def handler(ctx: CommandContext) -> dict[str, str]:
        calls.append((ctx.command.command or "", ctx.target.target.id if ctx.target else None))
        return {"seen": ctx.command.command or ""}

    spec = HandlerSpec("test-tool", HandlerKind.COMMAND, handler, None, None, None)
    job = _make_job(
        commands=[Command(type="test", command="c1"), Command(type="test", command="c2")],
        targets=[_target("t1"), _target("t2")],
    )

    result = await run_job(job, spec, _noop_progress)

    assert calls == [("c1", "t1"), ("c2", "t1"), ("c1", "t2"), ("c2", "t2")]
    assert result.state is JobState.SUCCESS
    assert result.variables["seen"].value == "c2"  # last-write-wins


async def test_command_handler_zero_targets_runs_once_per_command() -> None:
    calls: list[str | None] = []

    async def handler(ctx: CommandContext) -> None:
        calls.append(ctx.target.target.id if ctx.target else None)

    spec = HandlerSpec("test-tool", HandlerKind.COMMAND, handler, None, None, None)
    job = _make_job(commands=[Command(type="test", command="c1")], targets=[])

    result = await run_job(job, spec, _noop_progress)

    assert calls == [None]
    assert result.state is JobState.SUCCESS
    assert result.target_results is None


async def test_command_handler_aborts_target_on_failure_but_continues_others() -> None:
    calls: list[tuple[str, str | None]] = []

    async def handler(ctx: CommandContext) -> None:
        target_id = ctx.target.target.id if ctx.target else None
        calls.append((ctx.command.command or "", target_id))
        if target_id == "t1" and ctx.command.command == "c1":
            raise FinJobError("boom")

    spec = HandlerSpec("test-tool", HandlerKind.COMMAND, handler, None, None, None)
    job = _make_job(
        commands=[Command(type="test", command="c1"), Command(type="test", command="c2")],
        targets=[_target("t1"), _target("t2")],
    )

    result = await run_job(job, spec, _noop_progress)

    # t1's second command never runs, but t2 still runs both commands.
    assert calls == [("c1", "t1"), ("c1", "t2"), ("c2", "t2")]
    assert result.state is JobState.FAILURE
    assert result.error == "boom"
    assert result.target_results is not None
    assert len(result.target_results) == 2
    assert result.target_results[0].state is JobState.FAILURE
    assert result.target_results[0].failed_command_index == 0
    assert result.target_results[1].state is JobState.SUCCESS


async def test_command_handler_plain_exception_fails_target() -> None:
    async def handler(ctx: CommandContext) -> None:
        raise ValueError("unexpected")

    spec = HandlerSpec("test-tool", HandlerKind.COMMAND, handler, None, None, None)
    job = _make_job(commands=[Command(type="test", command="c1")], targets=[_target("t1")])

    result = await run_job(job, spec, _noop_progress)

    assert result.state is JobState.FAILURE
    assert result.error == "unexpected"


async def test_step_handler_receives_full_job_and_returns_variables() -> None:
    async def handler(ctx: StepContext) -> dict[str, str]:
        assert len(ctx.commands) == 2
        assert len(ctx.targets) == 1
        return {"result": "ok"}

    spec = HandlerSpec("test-tool", HandlerKind.STEP, handler, None, None, None)
    job = _make_job(
        commands=[Command(type="test", command="c1"), Command(type="test", command="c2")],
        targets=[_target("t1")],
    )

    result = await run_job(job, spec, _noop_progress)

    assert result.state is JobState.SUCCESS
    assert result.variables["result"].value == "ok"


async def test_step_handler_raising_fin_job_error_fails_with_variables() -> None:
    async def handler(ctx: StepContext) -> None:
        raise FinJobError("partial failure", variables={"partial": "data"})

    spec = HandlerSpec("test-tool", HandlerKind.STEP, handler, None, None, None)
    job = _make_job(commands=[], targets=[])

    result = await run_job(job, spec, _noop_progress)

    assert result.state is JobState.FAILURE
    assert result.error == "partial failure"
    assert result.variables["partial"].value == "data"


async def test_step_handler_can_return_step_result_with_target_results() -> None:
    async def handler(ctx: StepContext) -> StepResult:
        return StepResult(variables={"a": "b"}, target_results=[])

    spec = HandlerSpec("test-tool", HandlerKind.STEP, handler, None, None, None)
    job = _make_job(commands=[], targets=[])

    result = await run_job(job, spec, _noop_progress)

    assert result.state is JobState.SUCCESS
    assert result.variables["a"].value == "b"
    assert result.target_results == []


async def test_step_handler_receives_bound_logger_with_job_context() -> None:
    seen: dict[str, object] = {}

    async def handler(ctx: StepContext) -> None:
        seen["log"] = ctx.log
        seen["extra"] = dict(ctx.log.extra or {})

    spec = HandlerSpec("test-tool", HandlerKind.STEP, handler, None, None, None)
    job = _make_job(commands=[], targets=[])

    await run_job(job, spec, _noop_progress)

    assert isinstance(seen["log"], logging.LoggerAdapter)
    extra = seen["extra"]
    assert extra["job_id"] == str(job.job_id)
    assert extra["execution_id"] == str(job.execution_id)
    assert extra["step_id"] == job.step_id
    assert extra["capability_type"] == job.capability_type


async def test_command_handler_logger_includes_target_and_command_index() -> None:
    seen: list[dict[str, object]] = []

    async def handler(ctx: CommandContext) -> None:
        seen.append(dict(ctx.log.extra or {}))

    spec = HandlerSpec("test-tool", HandlerKind.COMMAND, handler, None, None, None)
    job = _make_job(
        commands=[Command(type="test", command="c1"), Command(type="test", command="c2")],
        targets=[_target("t1"), _target("t2")],
    )

    await run_job(job, spec, _noop_progress)

    assert [(extra["target_index"], extra["command_index"]) for extra in seen] == [
        (0, 0),
        (0, 1),
        (1, 0),
        (1, 1),
    ]
    # base job context is still present on every per-command logger
    assert all(extra["job_id"] == str(job.job_id) for extra in seen)


def test_context_logger_prefixes_messages_with_bound_fields(caplog) -> None:  # type: ignore[no-untyped-def]
    async def handler(ctx: CommandContext) -> None:
        ctx.log.info("hello")

    spec = HandlerSpec("test-tool", HandlerKind.COMMAND, handler, None, None, None)
    job = _make_job(commands=[Command(type="test", command="c1")], targets=[_target("t1")])

    with caplog.at_level(logging.INFO, logger="soarca_fin.handler"):
        asyncio.run(run_job(job, spec, _noop_progress))

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert f"job_id={job.job_id}" in message
    assert "target_index=0" in message
    assert "command_index=0" in message
    assert message.endswith("hello")


def test_sync_handler_is_supported() -> None:
    def handler(ctx: CommandContext) -> dict[str, str]:
        return {"sync": "yes"}

    spec = HandlerSpec("test-tool", HandlerKind.COMMAND, handler, None, None, None)
    job = _make_job(commands=[Command(type="test", command="c1")], targets=[])

    result = asyncio.run(run_job(job, spec, _noop_progress))

    assert result.state is JobState.SUCCESS
    assert result.variables["sync"].value == "yes"
