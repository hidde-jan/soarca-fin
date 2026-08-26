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

fin = Fin("http://localhost:8080")


@fin.command("my-tool", description="Runs my-tool against a target")
async def run_my_tool(ctx: CommandContext) -> dict[str, str]:
    target = ctx.target.target if ctx.target else None
    print(f"running {ctx.command.command!r} against {target.name if target else 'no target'}")
    return {"output": "some result"}
```

Save that as `my_fin.py`, then drive it with the `soarca-fin` CLI (installed
alongside the library, similar to Flask's own `flask --app hello run`) -
`--app` follows Flask's `MODULE[:ATTRIBUTE]` convention and can also be set
via `SOARCA_FIN_APP` instead of passing `--app` every time:

```bash
soarca-fin --app my_fin:fin register --token my-registration-secret  # once
soarca-fin --app my_fin:fin run  # every subsequent start
```

Registration is a separate, explicit, one-time step - it is deliberately
*not* part of `run()`, and never happens implicitly, so a plain
`if __name__ == "__main__":` block that calls both `register()` and `run()`
would be wrong (it would try to register a new Fin identity on every
restart). Use the CLI's two separate subcommands instead, as shown above.
If `--token` is omitted, `register` falls back to the
`SOARCA_FIN_REGISTRATION_TOKEN` environment variable, then an interactive
prompt.

`register()` stores the issued `fin_id`/`fin_token` at
`~/.soarca-fin/registration.json`, so `run()` finds them automatically on
every future start - re-registering is never required (and `run()` will
raise if it can't find a prior registration, rather than registering an
unwanted new Fin identity for you). `registration_token` itself is never
persisted; it's only a one-time bootstrap secret used for that one call.

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

fin = Fin("http://localhost:8080")


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

## Registration storage

A successful registration produces a `FinRegistration`: the `fin_id`/
`fin_token` used to authenticate, plus the operational parameters SOARCA
chose for this Fin (`poll_interval_seconds`, `long_poll_timeout_seconds`,
`job_lease_seconds`). By default this is persisted to
`~/.soarca-fin/registration.json` (owner-only permissions). Pass a different
store to customize this:

```python
from soarca_fin import Fin, FileRegistrationStore, InMemoryRegistrationStore

# custom path
fin = Fin(..., registration_store=FileRegistrationStore("/etc/my-fin/registration.json"))

# no persistence - re-registers as a new Fin identity on every restart
fin = Fin(..., registration_store=InMemoryRegistrationStore())
```

Implement the `RegistrationStore` protocol yourself to use a secrets
manager, database, etc.

You can also skip the registration store entirely and hand `run()` a
`fin_token` you manage yourself (e.g. obtained out-of-band and kept in your
own secrets store):

```python
fin.run(fin_token="already-known-token")
```

## Concurrency

```python
fin = Fin("http://localhost:8080", concurrency=4)
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
