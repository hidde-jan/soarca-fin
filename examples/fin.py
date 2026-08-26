"""Example Fin: an ``ssh-runner`` capability, runnable end-to-end against a
local SOARCA instance.

Run it (from this directory, so ``soarca-fin`` finds this file by default):

    soarca-fin register --token my-registration-secret  # once
    soarca-fin run  # every subsequent run

See the project README for the full walkthrough (registration storage,
progress reporting, logging, unregistering, etc).
"""

from __future__ import annotations

from soarca_fin import CommandContext, Fin, FinJobError, StepContext

fin = Fin("http://localhost:8080", display_name="example-ssh-fin")


@fin.command(
    "ssh-runner",
    description="Runs a shell command over SSH against one target",
    version="1.0.0",
    examples=[
        {
            "type": "action",
            "name": "Restart the nginx service",
            "agent": "ssh-runner--f3f0194f-99e6-4966-8512-de3806fecfdf",
            "commands": [{"type": "manual", "command": "sudo systemctl restart nginx"}],
        }
    ],
)
async def run_ssh(ctx: CommandContext) -> dict[str, str]:
    if ctx.target is None:
        raise FinJobError("ssh-runner requires a target")

    ctx.log.info("connecting to %s", ctx.target.target.name)
    await ctx.report_progress(f"connecting to {ctx.target.target.name}")

    ...  # your actual SSH logic here

    ctx.log.debug("command %r completed", ctx.command.command)
    return {"output": "some result"}


@fin.step(
    "ssh-batch-runner",
    description="Runs a shell command over SSH against every target in the step",
    version="1.0.0",
)
async def run_ssh_batch(ctx: StepContext) -> dict[str, str]:
    """Full-step handler: called once per job with every command/target,
    for when you want to control aggregation yourself (e.g. one SSH
    connection reused across all targets) instead of the default
    one-call-per-(command, target) behaviour of ``@fin.command``."""
    for target in ctx.targets:
        ctx.log.info("connecting to %s", target.target.name)
        await ctx.report_progress(f"connecting to {target.target.name}")
        for command in ctx.commands:
            ...  # your actual SSH logic here
            ctx.log.debug("ran %r on %s", command.command, target.target.name)

    return {"output": "all targets completed"}
