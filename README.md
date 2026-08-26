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

## Registering

A complete, runnable example - a `fin.py` with a single `ssh-runner`
capability:

```python
# fin.py
from soarca_fin import CommandContext, Fin, FinJobError

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

    await ctx.report_progress(f"connecting to {ctx.target.target.name}")
    ...  # your actual SSH logic here
    return {"output": "some result"}
```

`examples` is optional and purely illustrative - full CACAO action steps
shown to playbook authors (e.g. in SOARCA's admin UI) to demonstrate how to
invoke this capability. SOARCA never interprets or validates them.

Before registering for real, use `--dry-run` to see exactly what would be
sent to `POST /fin/register` - the capabilities are derived from your
`@fin.step`/`@fin.command` decorators, so this is the easiest way to check
they look right (e.g. the right `type`/`description`/`version`/`examples`)
before committing to an identity:

```console
$ soarca-fin register --token my-registration-secret --dry-run
{
  "registration_token": "my-registration-secret",
  "display_name": "example-ssh-fin",
  "protocol_version": null,
  "capabilities": [
    {
      "type": "ssh-runner",
      "description": "Runs a shell command over SSH against one target",
      "version": "1.0.0",
      "step_examples": [
        {
          "type": "action",
          "name": "Restart the nginx service",
          "agent": "ssh-runner--f3f0194f-99e6-4966-8512-de3806fecfdf",
          "commands": [
            {
              "type": "manual",
              "command": "sudo systemctl restart nginx"
            }
          ]
        }
      ]
    }
  ]
}
```

`--dry-run` never contacts SOARCA and never touches `registration_store` -
it just builds and prints the request body. Once it looks right, register
for real (this one-time step is never repeated automatically - see below):

```console
$ soarca-fin register --token my-registration-secret
registered as fin_id=fin-a1b2c3
$ soarca-fin run
```

If `--token` is omitted, `register` falls back to the
`SOARCA_FIN_REGISTRATION_TOKEN` environment variable, then an interactive
prompt (so the secret never has to appear in your shell history).

Registration is a separate, explicit, one-time step - it is deliberately
*not* part of `run()`, and never happens implicitly, so a plain
`if __name__ == "__main__":` block that calls both `register()` and `run()`
would be wrong (it would try to register a new Fin identity on every
restart). Use the CLI's two separate subcommands instead, as shown above.

`register` stores the issued `fin_id`/`fin_token` at
`~/.soarca-fin/registration.json`, so `run` finds them automatically on
every future start - re-registering is never required (and `run` will raise
if it can't find a prior registration, rather than registering an unwanted
new Fin identity for you). `registration_token` itself is never persisted;
it's only a one-time bootstrap secret used for that one call.

Like Flask, `--app` is optional: if the module is named `fin.py` or `app.py`
and lives in the current directory, and the `Fin` instance is assigned to a
module-level variable named `fin` or `app`, soarca-fin finds it
automatically. Otherwise pass `--app MODULE[:ATTRIBUTE]` (e.g.
`--app my_fin:fin`), or set the `SOARCA_FIN_APP` environment variable
instead of passing `--app` every time.

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

Programmatically, `fin.build_register_request(token)` gives you the same
dry-run inspection the CLI's `--dry-run` flag uses - handy in a test or a
`python -c "..."` one-liner without needing a running SOARCA instance:

```python
print(fin.build_register_request("my-registration-secret").model_dump_json(indent=2))
```

## Unregistering

```bash
soarca-fin unregister
```

Removes this Fin's registration from SOARCA and clears the local
`registration_store`. By default it acts on the stored registration; pass
both `--fin-id` and `--fin-token` to unregister a different Fin identity
instead (e.g. one you're cleaning up after losing the local store) - a Fin
can only unregister itself, so mismatched credentials are rejected by
SOARCA. Programmatically:

```python
fin.unregister()  # uses the stored registration
fin.unregister(fin_id="fin-1", fin_token="already-known-token")  # explicit
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
