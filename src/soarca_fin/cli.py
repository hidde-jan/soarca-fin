"""Command-line interface for soarca_fin, modeled after Flask's own
``flask --app hello run``.

Usage::

    soarca-fin --app my_fin:fin register
    soarca-fin --app my_fin:fin run

``--app`` follows Flask's convention: ``MODULE[:ATTRIBUTE]``. If
``ATTRIBUTE`` is omitted, a module-level variable named ``fin`` is used
(falling back to the only :class:`~soarca_fin.app.Fin` instance found at
module level, if there is exactly one). Can also be set via the
``SOARCA_FIN_APP`` environment variable instead of passing ``--app`` every
time.

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
import os

from soarca_fin.app import Fin


def _load_fin(app_spec: str) -> Fin:
    module_name, _, attr_name = app_spec.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        raise SystemExit(f"could not import {module_name!r}: {error}") from error

    if attr_name:
        obj = getattr(module, attr_name, None)
        if obj is None:
            raise SystemExit(f"{module_name!r} has no attribute {attr_name!r}")
    else:
        obj = getattr(module, "fin", None)
        if obj is None:
            candidates = [value for value in vars(module).values() if isinstance(value, Fin)]
            if len(candidates) == 1:
                obj = candidates[0]
            elif not candidates:
                raise SystemExit(
                    f"no Fin instance found in {module_name!r} - define one named `fin`, "
                    f"or pass --app {module_name}:<name>"
                )
            else:
                raise SystemExit(
                    f"multiple Fin instances found in {module_name!r} - "
                    f"pass --app {module_name}:<name> to disambiguate"
                )

    if not isinstance(obj, Fin):
        raise SystemExit(f"{app_spec!r} is not a soarca_fin.Fin instance")
    return obj


def _resolve_app_spec(args_app: str | None) -> str:
    app_spec = args_app or os.environ.get("SOARCA_FIN_APP")
    if not app_spec:
        raise SystemExit("no app specified - pass --app MODULE[:ATTRIBUTE] or set SOARCA_FIN_APP")
    return app_spec


def _cmd_register(fin: Fin, args: argparse.Namespace) -> None:
    token = args.token or os.environ.get("SOARCA_FIN_REGISTRATION_TOKEN")
    if not token:
        token = getpass.getpass("Registration token: ")
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="soarca-fin")
    parser.add_argument(
        "--app",
        help="MODULE[:ATTRIBUTE] pointing at your Fin instance, e.g. my_fin:fin "
        "(defaults to the SOARCA_FIN_APP environment variable)",
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

    run_parser = subparsers.add_parser("run", help="poll for and execute jobs")
    run_parser.add_argument(
        "--fin-token",
        help="use this fin_token directly instead of a stored registration "
        "(defaults to SOARCA_FIN_TOKEN if set)",
    )
    run_parser.add_argument("--poll-interval", type=int, default=None, metavar="SECONDS")
    run_parser.add_argument("--long-poll-timeout", type=int, default=None, metavar="SECONDS")
    run_parser.add_argument("--job-lease-seconds", type=int, default=None, metavar="SECONDS")

    return parser


def main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    fin = _load_fin(_resolve_app_spec(args.app))

    if args.command == "register":
        _cmd_register(fin, args)
    elif args.command == "run":
        _cmd_run(fin, args)


if __name__ == "__main__":
    main()
