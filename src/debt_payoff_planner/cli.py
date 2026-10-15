"""Noninteractive JSON CLI; all monetary input uses decimal strings."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from typing import Any
from zoneinfo import ZoneInfoNotFoundError

import yaml

from . import __version__
from .config import make_planner
from .core.calculations import daily_apr, fee_apr
from .core.reports import report
from .core.simulation import compare, extra, progress, simulate
from .core.store import NotFound, Planner


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def parser() -> Parser:
    root = Parser(
        prog="debt", description="Offline debt payoff planning and actual payment tracking"
    )
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    add = commands.add_parser("add")
    for key in ("code", "type", "balance"):
        add.add_argument("--" + key, required=True)
    for key, default in (
        ("annual-rate", "0"),
        ("daily-rate", None),
        ("minimum", "0"),
        ("start-date", None),
        ("maturity-date", None),
        ("note", ""),
        ("fee-rate", "0"),
        ("fee-policy", "retain"),
        ("minimum-ratio", "0.1"),
        ("grace-until", None),
    ):
        add.add_argument("--" + key, default=default)
    add.add_argument("--term-months", type=int)
    add.add_argument("--due-day", type=int, default=15)
    add.add_argument("--priority", type=int, default=100)
    commands.add_parser("list")
    update = commands.add_parser("update")
    update.add_argument("id")
    update.add_argument("--data", required=True, help="JSON object of editable fields")
    pay = commands.add_parser("pay")
    pay.add_argument("id")
    pay.add_argument("--amount", required=True)
    pay.add_argument("--date", required=True)
    pay.add_argument(
        "--interest", help="Newly assessed statement finance charges; replaces estimated accrual"
    )
    pay.add_argument("--note", default="")
    pay.add_argument("--idempotency-key")
    commands.add_parser("payments")
    budget = commands.add_parser("budget").add_subparsers(dest="action", required=True)
    budget.add_parser("list")
    set_budget = budget.add_parser("set")
    set_budget.add_argument("month")
    set_budget.add_argument("--amount", required=True)
    for name in ("simulate", "compare", "extra", "report"):
        child = commands.add_parser(name)
        child.add_argument("--budget")
        child.add_argument("--start")
        child.add_argument("--months", type=int, default=600)
        if name in {"simulate", "extra"}:
            child.add_argument("--strategy", default="avalanche")
        if name in {"simulate", "compare"}:
            child.add_argument(
                "--order", help="Comma-separated debt codes, used for custom strategy"
            )
        if name == "simulate":
            child.add_argument("--save", action="store_true")
        elif name == "extra":
            child.add_argument("--amount", required=True)
            child.add_argument("--month", required=True)
        elif name == "report":
            child.add_argument("--md", action="store_true", help="Markdown is the default output")
            child.add_argument("--out")
    due = commands.add_parser("due")
    due.add_argument("--within", default="7d")
    due.add_argument("--as-of")
    commands.add_parser("plans")
    prog = commands.add_parser("progress")
    prog.add_argument("--plan", required=True)
    prog.add_argument("--as-of")
    apr = commands.add_parser("apr")
    group = apr.add_mutually_exclusive_group(required=True)
    group.add_argument("--fee-rate")
    group.add_argument("--daily-rate")
    apr.add_argument("--months", type=int, default=12)
    apr.add_argument("--fee-mode", choices=["monthly", "total", "upfront"], default="monthly")
    imp = commands.add_parser("import")
    imp.add_argument("path")
    exp = commands.add_parser("export")
    exp.add_argument("--format", choices=["json", "csv"], default="json")
    exp.add_argument("--out", required=True)
    exp.add_argument("--utf8", action="store_true")
    backup = commands.add_parser("backup")
    backup.add_argument("--out")
    serve = commands.add_parser("serve")
    serve.add_argument("--host", choices=["127.0.0.1", "localhost", "::1"], default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8768)
    commands.add_parser("mcp")
    return root


def execute(a: argparse.Namespace, store: Planner) -> Any:
    cmd = a.command
    if cmd == "init":
        return {"database": str(store.path), "currency": store.currency, "timezone": store.timezone}
    if cmd == "add":
        return store.add(**{k: v for k, v in vars(a).items() if k != "command"})
    if cmd == "list":
        return store.list_debts()
    if cmd == "update":
        return store.update(a.id, json.loads(a.data))
    if cmd == "pay":
        return store.pay(a.id, a.amount, a.date, a.interest, a.note, a.idempotency_key)
    if cmd == "payments":
        return store.payments()
    if cmd == "budget":
        return store.budgets() if a.action == "list" else store.set_budget(a.month, a.amount)
    if cmd == "simulate":
        return simulate(
            store,
            a.strategy,
            a.budget,
            a.start,
            a.months,
            a.order.split(",") if a.order else None,
            a.save,
        )
    if cmd == "compare":
        return compare(store, a.budget, a.start, a.months, a.order.split(",") if a.order else None)
    if cmd == "extra":
        return extra(store, a.amount, a.month, a.budget, a.start, a.strategy, a.months)
    if cmd == "report":
        return report(store, a.budget, a.start, a.months, a.out)
    if cmd == "due":
        if not re.fullmatch(r"\d+d", a.within):
            raise ValueError("within must use a day count, for example 7d")
        return store.due(int(a.within[:-1]), a.as_of)
    if cmd == "plans":
        return store.plans()
    if cmd == "progress":
        return progress(store, a.plan, a.as_of)
    if cmd == "apr":
        return (
            daily_apr(a.daily_rate)
            if a.daily_rate is not None
            else fee_apr(a.fee_rate, a.months, a.fee_mode)
        )
    if cmd == "import":
        return store.import_file(a.path)
    if cmd == "export":
        return store.export(a.out, a.format, not a.utf8)
    if cmd == "backup":
        return store.backup(a.out)
    raise ValueError("unknown command")


def failure(code: str, message: str, status: int) -> int:
    print(json.dumps({"ok": False, "error": {"code": code, "message": message}}))
    return status


def main(argv: list[str] | None = None) -> int:
    try:
        common = Parser(add_help=False, allow_abbrev=False)
        common.add_argument("--data-dir")
        common.add_argument("--config")
        for option in ("json", "quiet", "progress", "version"):
            common.add_argument("--" + option, action="store_true")
        options, remaining = common.parse_known_args(sys.argv[1:] if argv is None else argv)
        if options.version:
            print(__version__)
            return 0
        if not remaining or remaining == ["--help"]:
            parser().print_help()
            print("\nGlobal: --data-dir DIR --config YAML --json --quiet --progress --version")
            return 0
        a = parser().parse_args(remaining)
        store = make_planner(options.data_dir, options.config)
        if a.command == "serve":
            import uvicorn

            from .web.app import create_app

            uvicorn.run(create_app(store), host=a.host, port=a.port)
            return 0
        if a.command == "mcp":
            from .mcp_server import create_server

            create_server(store).run(transport="stdio")
            return 0
        if options.progress:
            print(f"Running {a.command}...", file=sys.stderr)
        result = execute(a, store)
        if options.json or not options.quiet:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "data": result,
                        "warnings": result.get("warnings", []) if isinstance(result, dict) else [],
                    }
                )
            )
        return 0
    except NotFound as exc:
        return failure("NOT_FOUND", str(exc), 3)
    except (
        ValueError,
        TypeError,
        KeyError,
        yaml.YAMLError,
        ZoneInfoNotFoundError,
        sqlite3.IntegrityError,
    ) as exc:
        return failure("INVALID_ARGUMENT", str(exc), 2)
    except (OSError, sqlite3.Error) as exc:
        return failure("IO_ERROR", str(exc), 1)


if __name__ == "__main__":
    raise SystemExit(main())
