"""Command-line interface for soarca_fin, modeled after Flask's own
``flask --app hello run``.

Usage::

    soarca-fin register
    soarca-fin run

Like Flask, ``--app`` is optional. If omitted (and ``SOARCA_FIN_APP`` isn't
set either), soarca-fin looks for ``fin.py`` then ``app.py`` in the current
directory - so the common case needs no flag at all. When needed, ``--app``
follows Flask's convention: ``MODULE[:ATTRIBUTE]``. If ``ATTRIBUTE`` is
omitted, a module-level variable named ``fin`` (then ``app``) is used,
falling back to the only :class:`~soarca_fin.app.Fin` instance found at
module level if there is exactly one. ``SOARCA_FIN_APP`` can be set instead
of passing ``--app`` every time, e.g. for a Fin that doesn't live in
``fin.py``/``app.py``.

This exists specifically so registration and running are two clearly
separate invocations - never both together in one script run - which is
easy to get wrong by hand (e.g. a naive ``if __name__ == "__main__":``
block that calls both ``fin.register(...)`` and ``fin.run()`` would
re-register a brand new Fin identity on every single restart).
"""

from __future__ import annotations

import argparse
import getpass
import importlib
import logging
import os
import sys
from pathlib import Path

from soarca_fin.app import Fin

_DEFAULT_APP_FILENAMES = ("fin.py", "app.py", "main.py")
_DEFAULT_APP_ATTRS = ("fin", "app")


def _find_fin_instance(module: object, module_name: str) -> Fin:
    for attr_name in _DEFAULT_APP_ATTRS:
        obj = getattr(module, attr_name, None)
        if isinstance(obj, Fin):
            return obj

    candidates = [value for value in vars(module).values() if isinstance(value, Fin)]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise SystemExit(
            f"no Fin instance found in {module_name!r} - define one named `fin`, "
            f"or pass --app {module_name}:<name>"
        )
    raise SystemExit(
        f"multiple Fin instances found in {module_name!r} - "
        f"pass --app {module_name}:<name> to disambiguate"
    )


def _import_module(module_name: str) -> object:
    # Mirror Flask's behaviour: make the current directory importable so a
    # bare `fin.py`/`app.py` next to where the CLI is invoked is found, even
    # though it isn't installed as a package.
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    try:
        return importlib.import_module(module_name)
    except ImportError as error:
        raise SystemExit(f"could not import {module_name!r}: {error}") from error


def _load_fin(app_spec: str) -> Fin:
    module_name, _, attr_name = app_spec.partition(":")
    module = _import_module(module_name)

    if attr_name:
        obj = getattr(module, attr_name, None)
        if obj is None:
            raise SystemExit(f"{module_name!r} has no attribute {attr_name!r}")
    else:
        obj = _find_fin_instance(module, module_name)

    if not isinstance(obj, Fin):
        raise SystemExit(f"{app_spec!r} is not a soarca_fin.Fin instance")
    return obj


def _discover_default_app_spec() -> str | None:
    cwd = Path.cwd()
    for filename in _DEFAULT_APP_FILENAMES:
        if (cwd / filename).is_file():
            return filename.removesuffix(".py")
    return None


def _resolve_app_spec(args_app: str | None) -> str:
    app_spec = args_app or os.environ.get("SOARCA_FIN_APP") or _discover_default_app_spec()
    if not app_spec:
        raise SystemExit(
            "no app found - pass --app MODULE[:ATTRIBUTE], set SOARCA_FIN_APP, "
            "or run from a directory containing fin.py or app.py"
        )
    return app_spec


def _cmd_register(fin: Fin, args: argparse.Namespace) -> None:
    token = args.token or os.environ.get("SOARCA_FIN_REGISTRATION_TOKEN")
    if not token:
        token = getpass.getpass("Registration token: ")

    if args.dry_run:
        request = fin.build_register_request(token)
        print(request.model_dump_json(indent=2))  # noqa: T201
        return

    registration = fin.register(token)
    print(f"registered as fin_id={registration.fin_id}")  # noqa: T201


def _cmd_run(fin: Fin, args: argparse.Namespace) -> None:
    fin_token = args.fin_token or os.environ.get("SOARCA_FIN_TOKEN")
    kwargs: dict[str, object] = {}
    if fin_token:
        kwargs["fin_token"] = fin_token
    if args.poll_interval is not None:
        kwargs["poll_interval_seconds"] = args.poll_interval
    if args.long_poll_timeout is not None:
        kwargs["long_poll_timeout_seconds"] = args.long_poll_timeout
    if args.job_lease_seconds is not None:
        kwargs["job_lease_seconds"] = args.job_lease_seconds
    fin.run(**kwargs)  # type: ignore[arg-type]


def _cmd_unregister(fin: Fin, args: argparse.Namespace) -> None:
    fin_token = args.fin_token or os.environ.get("SOARCA_FIN_TOKEN")
    fin.unregister(fin_token=fin_token)
    print("unregistered fin from SOARCA")  # noqa: T201


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="soarca-fin")
    parser.add_argument(
        "--app",
        help="MODULE[:ATTRIBUTE] pointing at your Fin instance, e.g. my_fin:fin "
        "(defaults to the SOARCA_FIN_APP environment variable, then to fin.py "
        "or app.py in the current directory)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="root logging level for this CLI invocation (default: %(default)s). "
        "Use DEBUG to see per-command/target handler dispatch detail via "
        "soarca_fin.handler (see ctx.log in your handlers).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    register_parser = subparsers.add_parser(
        "register", help="register this Fin with SOARCA (one-time setup)"
    )
    register_parser.add_argument(
        "--token",
        help="registration token (defaults to SOARCA_FIN_REGISTRATION_TOKEN, "
        "or an interactive prompt if neither is given)",
    )
    register_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the exact JSON body that would be sent to POST /fin/register "
        "(including derived capabilities), without contacting SOARCA or storing "
        "anything",
    )

    run_parser = subparsers.add_parser("run", help="poll for and execute jobs")
    run_parser.add_argument(
        "--fin-token",
        help="use this fin_token directly instead of a stored registration "
        "(defaults to SOARCA_FIN_TOKEN if set)",
    )
    run_parser.add_argument("--poll-interval", type=int, default=None, metavar="SECONDS")
    run_parser.add_argument("--long-poll-timeout", type=int, default=None, metavar="SECONDS")
    run_parser.add_argument("--job-lease-seconds", type=int, default=None, metavar="SECONDS")

    unregister_parser = subparsers.add_parser(
        "unregister", help="remove this Fin's registration from SOARCA"
    )
    unregister_parser.add_argument(
        "--fin-token",
        help="authenticate as this fin_token instead of the stored registration "
        "(defaults to SOARCA_FIN_TOKEN)",
    )

    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    # A no-op if a handler is already configured (e.g. this Fin is embedded
    # in a larger application that manages its own logging setup).
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    fin = _load_fin(_resolve_app_spec(args.app))

    if args.command == "register":
        _cmd_register(fin, args)
    elif args.command == "run":
        _cmd_run(fin, args)
    elif args.command == "unregister":
        _cmd_unregister(fin, args)


if __name__ == "__main__":
    main()
