# soarca-fin

A Flask-like Python library for building [SOARCA](https://github.com/COSSAS/SOARCA)
Fins - external, pull-based executors for CACAO playbook steps.

It handles registration, long-polling, job leases/keepalives, and result
submission for you. You write handlers; the library deals with JSON, HTTP,
and timing.

## Install

```bash
uv add soarca-fin
```

## Quick start

The simplest Fin implements one handler per `(command, target)` pair - it is
called once for every command against every target in a step (and once with
`target=None` if the step declares no targets):

```python
from soarca_fin import Fin, CommandContext

fin = Fin("http://localhost:8080", registration_token="my-registration-secret")


@fin.command("my-tool", description="Runs my-tool against a target")
async def run_my_tool(ctx: CommandContext) -> dict[str, str]:
    target = ctx.target.target if ctx.target else None
    print(f"running {ctx.command.command!r} against {target.name if target else 'no target'}")
    return {"output": "some result"}


if __name__ == "__main__":
    fin.run()
```

Run it:

```bash
uv run python my_fin.py
```

On first run it registers with SOARCA using `registration_token` and stores
the issued credentials at `~/.soarca-fin/credentials.json`, so subsequent
restarts skip registration entirely.

Raise `soarca_fin.FinJobError("reason")` from a handler to fail that
command/target explicitly. Any other exception fails it too, using its
message. When a target's command fails, later commands for *that target* are
skipped, but other targets still run - other command/target pairs continue
running exactly like SOARCA's own built-in SSH capability.

## Full-step handlers

If you need more control - e.g. a single connection reused across commands,
or genuinely parallel target handling - register a handler for the whole
step instead:

```python
from soarca_fin import Fin, StepContext

fin = Fin("http://localhost:8080", registration_token="my-registration-secret")


@fin.step("my-tool")
async def run_step(ctx: StepContext) -> dict[str, str]:
    for target in ctx.targets:
        for command in ctx.commands:
            ...  # your own iteration/aggregation logic
    return {"output": "some result"}
```

Return a `dict` of variables (or `None`) for the common case, or a
`soarca_fin.StepResult(variables=..., target_results=...)` for full control
including per-target diagnostics.

## Sync handlers

Plain `def` handlers work too - they're run in a worker thread so a
blocking call (e.g. a blocking SSH library) doesn't stall other jobs:

```python
@fin.command("my-tool")
def run_my_tool(ctx: CommandContext) -> dict[str, str]:
    ...
    return {"output": "some result"}
```

## Progress reporting

Both `CommandContext` and `StepContext` expose `report_progress`, an async
callable for surfacing human-readable progress on long-running jobs. This is
entirely optional - the library keeps the job's lease alive automatically in
the background regardless of whether you call it:

```python
@fin.command("my-tool")
async def run_my_tool(ctx: CommandContext) -> dict[str, str]:
    await ctx.report_progress("starting up")
    ...
    return {"output": "done"}
```

## Credential storage

By default, credentials persist to `~/.soarca-fin/credentials.json` (owner-only
permissions). Pass a different store to customize this:

```python
from soarca_fin import Fin, FileCredentialStore, InMemoryCredentialStore

# custom path
fin = Fin(..., credential_store=FileCredentialStore("/etc/my-fin/credentials.json"))

# no persistence - re-registers as a new Fin identity on every restart
fin = Fin(..., credential_store=InMemoryCredentialStore())
```

Implement the `CredentialStore` protocol yourself to use a secrets manager,
database, etc.

## Concurrency

```python
fin = Fin("http://localhost:8080", registration_token="...", concurrency=4)
```

Runs up to 4 jobs at a time, sharing one connection pool.

## Development

```bash
uv sync
uv run pytest
uv run ruff format
uv run ruff check
uv run mypy src
```
