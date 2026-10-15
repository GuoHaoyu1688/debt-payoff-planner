"""Transactional local debt balances and an append-only actual payment journal."""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import tempfile
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .calculations import (
    TYPES,
    due_date,
    first_due,
    installment,
    minimum_due,
    month_distance,
    monthly_charge,
    parse_rate,
)
from .money import add_months, amount, cents, decimal, iso_date, month_date, rounded

SCHEMA = """
CREATE TABLE IF NOT EXISTS debts(id TEXT PRIMARY KEY, code TEXT UNIQUE NOT NULL, type TEXT NOT NULL,
 opening_balance_cents INTEGER NOT NULL, balance_cents INTEGER NOT NULL, annual_rate TEXT NOT NULL,
 minimum_cents INTEGER NOT NULL, due_day INTEGER NOT NULL, start_date TEXT NOT NULL, maturity_date TEXT,
 term_months INTEGER, priority INTEGER NOT NULL, note TEXT NOT NULL, fee_rate TEXT NOT NULL,
 fee_policy TEXT NOT NULL, minimum_ratio TEXT NOT NULL, grace_until TEXT,
 scheduled_payment_cents INTEGER NOT NULL, principal_step_cents INTEGER NOT NULL,
 unpaid_interest_cents INTEGER NOT NULL, future_fees_cents INTEGER NOT NULL,
 accrued_through TEXT, last_payment_date TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payments(id TEXT PRIMARY KEY, debt_id TEXT NOT NULL REFERENCES debts(id),
 date TEXT NOT NULL, amount_cents INTEGER NOT NULL, principal_cents INTEGER NOT NULL,
 interest_cents INTEGER NOT NULL, interest_override TEXT, note TEXT NOT NULL,
 before_json TEXT NOT NULL, after_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS budgets(month TEXT PRIMARY KEY, amount_cents INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, strategy TEXT NOT NULL, created_at TEXT NOT NULL, result_json TEXT NOT NULL);
"""


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class NotFound(LookupError):
    pass


class Planner:
    def __init__(
        self,
        data_dir: str | Path = "data",
        timezone: str = "Asia/Shanghai",
        currency: str = "CNY",
        default_budget: str | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "debts.db"
        ZoneInfo(timezone)
        if not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("currency must be an uppercase three-letter code")
        self.timezone, self.currency = timezone, currency
        self.default_budget = default_budget
        if default_budget is not None:
            cents(default_budget)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    def today(self) -> date:
        return datetime.now(ZoneInfo(self.timezone)).date()

    @contextmanager
    def connect(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _get(conn: sqlite3.Connection, identifier: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM debts WHERE id=? OR code=?", (identifier, identifier)
        ).fetchone()
        if row is None:
            raise NotFound("debt not found")
        return dict(row)

    @staticmethod
    def validate(data: dict[str, Any]) -> dict[str, Any]:
        row = dict(data)
        defaults = {
            "id": str(uuid.uuid4()),
            "annual_rate": "0",
            "minimum_cents": 0,
            "due_day": 15,
            "priority": 100,
            "note": "",
            "fee_rate": "0",
            "fee_policy": "retain",
            "minimum_ratio": "0.1",
            "grace_until": None,
            "maturity_date": None,
            "term_months": None,
            "unpaid_interest_cents": 0,
            "future_fees_cents": 0,
            "accrued_through": None,
            "last_payment_date": None,
            "created_at": timestamp(),
            "updated_at": timestamp(),
        }
        for key, value in defaults.items():
            row.setdefault(key, value)
        if (
            not isinstance(row.get("code"), str)
            or not row["code"].strip()
            or not isinstance(row["id"], str)
            or not row["id"]
        ):
            raise ValueError("debt code and id must be non-empty strings")
        if row.get("type") not in TYPES:
            raise ValueError("unsupported debt type")
        for key in (
            "opening_balance_cents",
            "balance_cents",
            "minimum_cents",
            "unpaid_interest_cents",
            "future_fees_cents",
        ):
            if type(row.get(key)) is not int or not 0 <= row[key] <= 9_000_000_000_000_000:
                raise ValueError(f"{key} must be a nonnegative integer within the supported range")
        if row["opening_balance_cents"] <= 0 or row["balance_cents"] > row["opening_balance_cents"]:
            raise ValueError(
                "opening principal must be positive and no smaller than current principal"
            )
        row["annual_rate"] = str(parse_rate(row["annual_rate"]))
        row["fee_rate"] = str(parse_rate(row["fee_rate"], "1"))
        row["minimum_ratio"] = str(parse_rate(row["minimum_ratio"], "1"))
        if type(row["due_day"]) is not int or not 1 <= row["due_day"] <= 31:
            raise ValueError("due_day must be from 1 to 31")
        if type(row["priority"]) is not int or row["priority"] < 0:
            raise ValueError("priority must be a nonnegative integer; lower numbers are paid first")
        row["start_date"] = iso_date(row["start_date"])
        if row["term_months"] is not None and (
            type(row["term_months"]) is not int or not 1 <= row["term_months"] <= 1200
        ):
            raise ValueError("term_months must be from 1 to 1200")
        if (
            row["type"] in {"equal_installment", "equal_principal", "fee_installment"}
            and not row["term_months"]
        ):
            raise ValueError("this installment debt requires term_months")
        if row["maturity_date"]:
            row["maturity_date"] = iso_date(row["maturity_date"])
            if row["maturity_date"] < first_due(row).isoformat():
                raise ValueError("maturity date cannot precede the first due date")
        elif row["term_months"]:
            row["maturity_date"] = due_date(
                row,
                add_months(first_due(row).replace(day=1), row["term_months"] - 1).strftime("%Y-%m"),
            ).isoformat()
        if row["type"] == "interest_only" and not row["maturity_date"]:
            raise ValueError("interest_only debt requires a maturity date or term")
        if (
            row["type"] == "fee_installment"
            and month_distance(first_due(row).isoformat(), row["maturity_date"]) + 1
            != row["term_months"]
        ):
            raise ValueError("fee installment maturity must match its number of monthly fees")
        if row["fee_policy"] not in {"retain", "waive"}:
            raise ValueError("fee_policy must be retain or waive")
        if row["type"] != "fee_installment" and (
            decimal(row["fee_rate"]) or row["future_fees_cents"]
        ):
            raise ValueError("flat fees apply only to fee_installment debt")
        if row["type"] == "fee_installment" and decimal(row["annual_rate"]):
            raise ValueError(
                "fee installments use fee_rate, not an additional annual interest rate"
            )
        if row["grace_until"]:
            row["grace_until"] = iso_date(row["grace_until"])
            if row["type"] != "credit_card":
                raise ValueError("grace_until applies only to credit cards")
        if row["accrued_through"]:
            month_date(row["accrued_through"])
        if row["last_payment_date"]:
            row["last_payment_date"] = iso_date(row["last_payment_date"])
        row.setdefault(
            "scheduled_payment_cents",
            installment(row["opening_balance_cents"], row["annual_rate"], row["term_months"])
            if row["type"] == "equal_installment"
            else 0,
        )
        row.setdefault(
            "principal_step_cents",
            rounded(decimal(row["opening_balance_cents"]) / row["term_months"])
            if row["term_months"]
            else 0,
        )
        if row["type"] == "fee_installment" and "future_fees_cents" not in data:
            row["future_fees_cents"] = (
                rounded(decimal(row["opening_balance_cents"]) * decimal(row["fee_rate"]))
                * row["term_months"]
            )
        for key in ("scheduled_payment_cents", "principal_step_cents"):
            if type(row[key]) is not int or row[key] < 0:
                raise ValueError(f"{key} must be a nonnegative integer")
        if row["future_fees_cents"] > 9_000_000_000_000_000:
            raise ValueError("total future fees exceed the supported monetary range")
        return row

    def add(
        self,
        code: str,
        type: str,
        balance: str,
        annual_rate: str = "0",
        daily_rate: str | None = None,
        minimum: str = "0",
        due_day: int = 15,
        start_date: str | None = None,
        maturity_date: str | None = None,
        term_months: int | None = None,
        priority: int = 100,
        note: str = "",
        fee_rate: str = "0",
        fee_policy: str = "retain",
        minimum_ratio: str = "0.1",
        grace_until: str | None = None,
    ) -> dict[str, Any]:
        if daily_rate is not None and parse_rate(annual_rate) != 0:
            raise ValueError("supply annual_rate or daily_rate, not both")
        annual = str(parse_rate(daily_rate, "1") * 365) if daily_rate is not None else annual_rate
        principal = cents(balance)
        data = dict(
            code=code,
            type=type,
            balance_cents=principal,
            opening_balance_cents=principal,
            annual_rate=annual,
            minimum_cents=cents(minimum),
            due_day=due_day,
            start_date=start_date or self.today().isoformat(),
            maturity_date=maturity_date,
            term_months=term_months,
            priority=priority,
            note=note,
            fee_rate=fee_rate,
            fee_policy=fee_policy,
            minimum_ratio=minimum_ratio,
            grace_until=grace_until,
        )
        row = self.validate(data)
        if type == "fee_installment":
            row["future_fees_cents"] = (
                rounded(decimal(principal) * decimal(row["fee_rate"])) * row["term_months"]
            )
        with self.connect(True) as conn:
            self._insert_row(conn, "debts", row)
        return row

    @staticmethod
    def _insert_row(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
        allowed = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        if set(row) - allowed:
            raise ValueError("unsupported fields: " + ", ".join(sorted(set(row) - allowed)))
        conn.execute(
            f'INSERT INTO "{table}" ({",".join(row)}) VALUES ({",".join("?" for _ in row)})',
            list(row.values()),
        )

    def update(self, identifier: str, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "annual_rate",
            "minimum",
            "due_day",
            "maturity_date",
            "priority",
            "note",
            "minimum_ratio",
            "grace_until",
            "fee_policy",
        }
        if not changes or set(changes) - allowed:
            raise ValueError(
                "update supports rate, minimum, due day, maturity, priority, note, ratio, grace and fee policy only"
            )
        with self.connect(True) as conn:
            old = self._get(conn, identifier)
            merged = {**old, **changes, "updated_at": timestamp()}
            if "minimum" in merged:
                merged["minimum_cents"] = cents(merged.pop("minimum"))
            if "annual_rate" in changes and old["type"] == "equal_installment":
                months = max(
                    1,
                    month_distance(
                        old["last_payment_date"] or old["start_date"], old["maturity_date"]
                    )
                    + 1,
                )
                merged["scheduled_payment_cents"] = installment(
                    old["balance_cents"], changes["annual_rate"], months
                )
            if merged["fee_policy"] == "waive" and merged["balance_cents"] == 0:
                merged["future_fees_cents"] = 0
            row = self.validate(merged)
            assignments = ",".join(f'"{key}"=?' for key in row if key != "id")
            conn.execute(
                f"UPDATE debts SET {assignments} WHERE id=?",
                [v for k, v in row.items() if k != "id"] + [row["id"]],
            )
            return row

    def list_debts(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM debts ORDER BY priority,code")]

    def payments(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute("SELECT * FROM payments ORDER BY date,created_at,id")
            ]

    @staticmethod
    def accrue(
        row: dict[str, Any], through: str, interest_override: str | None = None
    ) -> tuple[int, int]:
        """Return newly assessed charges and remaining unassessed flat fees."""
        fees = row["future_fees_cents"]
        if interest_override is not None:
            charge = cents(interest_override)
            return charge, max(0, fees - charge) if row["type"] == "fee_installment" else fees
        first = first_due(row).strftime("%Y-%m")
        if row["accrued_through"]:
            first = max(first, add_months(month_date(row["accrued_through"]), 1).strftime("%Y-%m"))
        periods = month_distance(first, through) + 1
        if periods > 1200:
            raise ValueError(
                "more than 1200 unrecorded months; provide an explicit statement interest override"
            )
        total = 0
        for index in range(max(0, periods)):
            month = add_months(month_date(first), index).strftime("%Y-%m")
            charge = monthly_charge(row, row["balance_cents"], month)
            if row["type"] == "fee_installment":
                charge = min(charge, fees)
                fees -= charge
            total += charge
        return total, fees

    def pay(
        self,
        identifier: str,
        value: str,
        on: str,
        interest: str | None = None,
        note: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        total, on = cents(value), iso_date(on)
        if total <= 0 or on > self.today().isoformat():
            raise ValueError("payment must be positive and cannot be future-dated")
        with self.connect(True) as conn:
            row = self._get(conn, identifier)
            key = idempotency_key or str(uuid.uuid4())
            prior = conn.execute("SELECT * FROM payments WHERE id=?", (key,)).fetchone()
            normalized_interest = amount(cents(interest)) if interest is not None else None
            if prior:
                same = (
                    prior["debt_id"],
                    prior["date"],
                    prior["amount_cents"],
                    prior["interest_override"],
                    prior["note"],
                ) == (row["id"], on, total, normalized_interest, note)
                if not same:
                    raise ValueError("idempotency key conflicts with another payment")
                return {**dict(prior), "deduplicated": True}
            if on < row["start_date"] or (
                row["last_payment_date"] and on < row["last_payment_date"]
            ):
                raise ValueError("payments must be chronological and on/after the opening date")
            charges, fees = self.accrue(row, on[:7], interest)
            due_interest = row["unpaid_interest_cents"] + charges
            maximum = (
                row["balance_cents"] + due_interest + (fees if row["fee_policy"] == "retain" else 0)
            )
            if total > maximum:
                raise ValueError(
                    "payment exceeds principal plus assessed charges and retained fees"
                )
            interest_paid = min(total, due_interest)
            principal_paid = min(row["balance_cents"], total - interest_paid)
            fee_prepaid = total - interest_paid - principal_paid
            fees -= fee_prepaid
            balance = row["balance_cents"] - principal_paid
            if balance == 0 and row["fee_policy"] == "waive":
                fees = 0
            after = {
                **row,
                "balance_cents": balance,
                "unpaid_interest_cents": due_interest - interest_paid,
                "future_fees_cents": fees,
                "accrued_through": on[:7],
                "last_payment_date": on,
                "updated_at": timestamp(),
            }
            conn.execute(
                "UPDATE debts SET balance_cents=?,unpaid_interest_cents=?,future_fees_cents=?,accrued_through=?,last_payment_date=?,updated_at=? WHERE id=?",
                (
                    balance,
                    after["unpaid_interest_cents"],
                    fees,
                    on[:7],
                    on,
                    after["updated_at"],
                    row["id"],
                ),
            )
            entry = dict(
                id=key,
                debt_id=row["id"],
                date=on,
                amount_cents=total,
                principal_cents=principal_paid,
                interest_cents=interest_paid + fee_prepaid,
                interest_override=normalized_interest,
                note=note,
                before_json=json.dumps(row),
                after_json=json.dumps(after),
                created_at=timestamp(),
            )
            self._insert_row(conn, "payments", entry)
            return {
                **entry,
                "balance_cents": balance,
                "unpaid_interest_cents": after["unpaid_interest_cents"],
                "future_fees_cents": fees,
                "allocation": "finance charges first, principal second, retained future fees last",
            }

    def set_budget(self, month: str, value: str) -> dict[str, Any]:
        month_date(month)
        row = {"month": month, "amount_cents": cents(value)}
        with self.connect(True) as conn:
            conn.execute(
                "INSERT INTO budgets VALUES (?,?) ON CONFLICT(month) DO UPDATE SET amount_cents=excluded.amount_cents",
                tuple(row.values()),
            )
        return row

    def budgets(self) -> dict[str, int]:
        with self.connect() as conn:
            return {
                r["month"]: r["amount_cents"]
                for r in conn.execute("SELECT * FROM budgets ORDER BY month")
            }

    def save_plan(self, result: dict[str, Any]) -> str:
        key = str(uuid.uuid4())
        with self.connect(True) as conn:
            conn.execute(
                "INSERT INTO plans VALUES (?,?,?,?)",
                (key, result["strategy"], timestamp(), json.dumps(result)),
            )
        return key

    def plans(self, identifier: str | None = None) -> Any:
        with self.connect() as conn:
            if identifier:
                row = conn.execute("SELECT * FROM plans WHERE id=?", (identifier,)).fetchone()
                if not row:
                    raise NotFound("saved plan not found")
                return json.loads(row["result_json"])
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT id,strategy,created_at FROM plans ORDER BY created_at DESC"
                )
            ]

    def due(self, days: int = 7, as_of: str | None = None) -> dict[str, Any]:
        if type(days) is not int or not 0 <= days <= 366:
            raise ValueError("days must be between 0 and 366")
        today = date.fromisoformat(iso_date(as_of)) if as_of else self.today()
        end = today + timedelta(days=days)
        rows = []
        payments = self.payments()
        for debt in self.list_debts():
            if (
                debt["balance_cents"] + debt["unpaid_interest_cents"] + debt["future_fees_cents"]
                <= 0
            ):
                continue
            for i in range(month_distance(today.isoformat(), end.isoformat()) + 1):
                month = add_months(today.replace(day=1), i).strftime("%Y-%m")
                on = due_date(debt, month)
                if debt["maturity_date"] and debt["maturity_date"][:7] == month:
                    on = min(on, date.fromisoformat(debt["maturity_date"]))
                if not today <= on <= end or on < first_due(debt):
                    continue
                charge, _ = self.accrue(debt, month)
                actual = [
                    p for p in payments if p["debt_id"] == debt["id"] and p["date"][:7] == month
                ]
                if actual:
                    opening = json.loads(actual[0]["before_json"])
                    initial_charge, _ = self.accrue(opening, month)
                    required = minimum_due(
                        opening,
                        opening["balance_cents"],
                        opening["unpaid_interest_cents"] + initial_charge,
                        month,
                    )
                    required = max(0, required - sum(p["amount_cents"] for p in actual))
                else:
                    required = minimum_due(
                        debt, debt["balance_cents"], debt["unpaid_interest_cents"] + charge, month
                    )
                rows.append(
                    {
                        "id": debt["id"],
                        "code": debt["code"],
                        "date": on.isoformat(),
                        "minimum_cents": required,
                        "maturity": bool(
                            debt["maturity_date"] and month >= debt["maturity_date"][:7]
                        ),
                    }
                )
        return {
            "as_of": today.isoformat(),
            "through": end.isoformat(),
            "payments": sorted(rows, key=lambda r: (r["date"], r["code"])),
            "currency": self.currency,
        }

    def export(self, out: str | Path, format: str = "json", bom: bool = True) -> dict[str, Any]:
        with self.connect() as conn:
            records = [
                {"record_type": table, **dict(row)}
                for table in ("debts", "payments", "budgets", "plans")
                for row in conn.execute(f'SELECT * FROM "{table}"')
            ]
        path = Path(out).expanduser().resolve()
        self._guard_output(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if format == "json":
            path.write_text(json.dumps(records, indent=2), encoding="utf-8")
        elif format == "csv":
            keys = (
                ["record_type"]
                + sorted(set().union(*(r.keys() for r in records)) - {"record_type"})
                if records
                else ["record_type"]
            )
            with path.open("w", encoding="utf-8-sig" if bom else "utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=keys)
                writer.writeheader()
                writer.writerows(records)
        else:
            raise ValueError("format must be json or csv")
        return {"path": str(path), "count": len(records), "format": format}

    def import_file(self, path: str | Path) -> dict[str, int]:
        source = Path(path)
        content = source.read_text(encoding="utf-8-sig")
        if source.suffix.lower() == ".json":
            records = json.loads(content)
        elif source.suffix.lower() == ".csv":
            records = list(csv.DictReader(io.StringIO(content)))
            for row in records:
                for key in list(row):
                    if row[key] == "":
                        del row[key]
                    elif key.endswith("_cents") or key in {"due_day", "term_months", "priority"}:
                        row[key] = int(row[key])
        else:
            raise ValueError("import requires JSON or CSV")
        if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
            raise ValueError("import must be an array of records")
        order = {"debts": 0, "payments": 1, "budgets": 2, "plans": 3}
        if any(r.get("record_type") not in order for r in records):
            raise ValueError("record_type must be debts, payments, budgets or plans")
        added = skipped = 0
        with self.connect(True) as conn:
            for source_row in sorted(records, key=lambda r: order[r["record_type"]]):
                row = dict(source_row)
                table = row.pop("record_type")
                if table == "debts":
                    if not row.get("id"):
                        raise ValueError("import records require stable IDs")
                    row = self.validate(row)
                elif table == "budgets":
                    month_date(row["month"])
                    if (
                        type(row.get("amount_cents")) is not int
                        or not 0 <= row["amount_cents"] <= 9_000_000_000_000_000
                    ):
                        raise ValueError("budget amount_cents must be a nonnegative integer")
                elif table == "payments":
                    if (
                        any(
                            type(row.get(k)) is not int or row[k] < 0
                            for k in ("amount_cents", "principal_cents", "interest_cents")
                        )
                        or row["amount_cents"] != row["principal_cents"] + row["interest_cents"]
                        or row["amount_cents"] <= 0
                    ):
                        raise ValueError("payment allocation must reconcile exactly")
                    row["date"] = iso_date(row["date"])
                    if row["date"] > self.today().isoformat():
                        raise ValueError("imported actual payment cannot be future-dated")
                    before, after = json.loads(row["before_json"]), json.loads(row["after_json"])
                    if (
                        before["id"] != row["debt_id"]
                        or after["id"] != row["debt_id"]
                        or before["balance_cents"] - after["balance_cents"]
                        != row["principal_cents"]
                    ):
                        raise ValueError("payment journal snapshots do not reconcile")
                    row.setdefault("interest_override", None)
                    row.setdefault("note", "")
                elif table == "plans":
                    result = json.loads(row["result_json"])
                    if result.get("strategy") != row["strategy"] or not isinstance(
                        result.get("rows"), list
                    ):
                        raise ValueError("invalid saved plan")
                key = "month" if table == "budgets" else "id"
                if not row.get(key):
                    raise ValueError("import records require stable IDs")
                old = conn.execute(
                    f'SELECT * FROM "{table}" WHERE "{key}"=?', (row[key],)
                ).fetchone()
                if old:
                    if any(
                        dict(old).get(k) != v
                        for k, v in row.items()
                        if k not in {"created_at", "updated_at"}
                    ):
                        raise ValueError(
                            "import ID conflicts with existing content; use an empty data directory for a snapshot restore"
                        )
                    skipped += 1
                else:
                    self._insert_row(conn, table, row)
                    added += 1
            # Export/import is a snapshot operation: payment principal is not applied a second time.
            for debt in conn.execute("SELECT * FROM debts"):
                latest = conn.execute(
                    "SELECT after_json FROM payments WHERE debt_id=? ORDER BY date DESC,created_at DESC,id DESC LIMIT 1",
                    (debt["id"],),
                ).fetchone()
                if latest and json.loads(latest[0])["balance_cents"] != debt["balance_cents"]:
                    raise ValueError("debt balance disagrees with its final payment snapshot")
        return {"inserted": added, "skipped": skipped}

    def _guard_output(self, path: Path) -> None:
        if path == self.path or str(path).startswith(str(self.path) + "-"):
            raise ValueError("output cannot overwrite the live database")

    def backup(self, out: str | Path | None = None) -> dict[str, Any]:
        path = (
            Path(out).expanduser().resolve()
            if out
            else self.data_dir / "backups" / f"debts_{datetime.now(UTC):%Y-%m-%d_%H%M%S_%f}.zip"
        )
        self._guard_output(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "debts.db"
            with self.connect() as source, sqlite3.connect(db) as target:
                source.backup(target)
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(db, "debts.db")
                archive.writestr(
                    "settings.json",
                    json.dumps(
                        {
                            "timezone": self.timezone,
                            "currency": self.currency,
                            "default_budget": self.default_budget,
                        }
                    ),
                )
        return {"path": str(path), "bytes": path.stat().st_size}
