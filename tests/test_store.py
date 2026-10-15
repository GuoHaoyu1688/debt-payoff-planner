import json
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from debt_payoff_planner.core.reports import report, summary
from debt_payoff_planner.core.simulation import compare, progress, simulate
from debt_payoff_planner.core.store import Planner


def loan(planner, **kwargs):
    return planner.add(
        "D-1",
        "personal",
        "1000",
        annual_rate="12%",
        minimum="100",
        start_date="2026-09-01",
        **kwargs,
    )


def test_actual_interest_override_and_idempotence(planner):
    loan(planner)
    first = planner.pay("D-1", "200", "2026-09-15", interest="8", idempotency_key="payment-1")
    assert first["principal_cents"] == 19200 and first["interest_cents"] == 800
    assert first["balance_cents"] == 80800
    assert planner.pay("D-1", "200", "2026-09-15", interest="8.00", idempotency_key="payment-1")[
        "deduplicated"
    ]
    with pytest.raises(ValueError):
        planner.pay("D-1", "201", "2026-09-15", interest="8", idempotency_key="payment-1")
    # Same month: interest is not charged twice.
    second = planner.pay("D-1", "100", "2026-09-20")
    assert second["interest_cents"] == 0 and second["balance_cents"] == 70800
    third = planner.pay("D-1", "100", "2026-10-10")
    assert third["interest_cents"] == 708 and third["balance_cents"] == 61508
    assert sum(p["principal_cents"] for p in planner.payments()) == 38492
    assert all(
        p["amount_cents"] == p["principal_cents"] + p["interest_cents"] for p in planner.payments()
    )


def test_missed_months_unpaid_charges_not_silently_ignored(planner):
    loan(planner)
    # September and October each accrue 10.00 on the unchanged 1,000 principal.
    payment = planner.pay("D-1", "5", "2026-10-01")
    assert payment["interest_cents"] == 500 and payment["principal_cents"] == 0
    assert payment["unpaid_interest_cents"] == 1500
    second = planner.pay("D-1", "100", "2026-10-02")
    assert second["interest_cents"] == 1500 and second["principal_cents"] == 8500
    with pytest.raises(ValueError):
        planner.pay("D-1", "100", "2026-09-30")
    with pytest.raises(ValueError):
        planner.pay("D-1", "100", "2027-01-01")
    with pytest.raises(ValueError):
        planner.pay("D-1", "10000", "2026-10-02")


def test_actual_payment_consumes_monthly_budget_and_minimum(planner):
    planner.add(
        "D-1", "personal", "1000", annual_rate="12%", minimum="100", start_date="2026-10-01"
    )
    planner.pay("D-1", "50", "2026-10-10")
    result = simulate(planner, "baseline", "100", start="2026-10", months=1)
    row = result["rows"][0]
    assert row["actual_paid_cents"] == 5000 and row["payment_cents"] == 5000
    assert row["interest_cents"] == 0 and row["balance_cents"] == 91000
    assert planner.due(7, "2026-10-10")["payments"][0]["minimum_cents"] == 5000
    assert (
        simulate(planner, "avalanche", "49", start="2026-10")["failure"]["code"]
        == "MINIMUM_BUDGET_SHORTFALL"
    )


def test_actual_fee_payments_and_waiver(planner):
    planner.add(
        "FEE", "fee_installment", "100", fee_rate="1%", term_months=3, start_date="2026-10-01"
    )
    first = planner.pay("FEE", "101", "2026-10-10")
    assert first["principal_cents"] == 10000 and first["future_fees_cents"] == 200
    assert simulate(planner, "baseline", "10", start="2026-11")["total_interest_cents"] == 200
    # Retained future fees can be prepaid explicitly; this does not reduce principal twice.
    last = planner.pay("FEE", "2", "2026-10-11")
    assert last["interest_cents"] == 200 and last["principal_cents"] == 0
    assert simulate(planner, "baseline", "0", start="2026-10")["complete"]


def test_concurrent_payment_key_and_overpayment_lock(planner):
    loan(planner)

    def record(_):
        return planner.pay("D-1", "100", "2026-10-01", interest="0", idempotency_key="same-key")

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(record, range(5)))
    assert len(planner.payments()) == 1 and planner.list_debts()[0]["balance_cents"] == 90000


def test_saved_plan_progress_ahead_behind_and_due_maturity(planner):
    planner.add("D-1", "personal", "300", minimum="100", start_date="2026-09-01")
    result = simulate(planner, "baseline", "100", start="2026-09", save=True)
    assert progress(planner, result["plan_id"], "2026-09-16")["debts"][0]["status"] == "behind"
    planner.pay("D-1", "300", "2026-09-10")
    actual = progress(planner, result["plan_id"], "2026-09-10")["debts"][0]
    assert actual["status"] == "ahead" and actual["days"] == 66  # Sep 10 to Nov 15.
    assert actual["actual_principal_cents"] == 0 and actual["principal_paid_percent"] == "100.00"
    assert len(planner.plans()) == 1
    planner.add(
        "BALLOON",
        "interest_only",
        "100",
        annual_rate="12%",
        start_date="2026-08-01",
        maturity_date="2026-10-12",
    )
    due = planner.due(7, "2026-10-06")["payments"]
    assert due[0]["date"] == "2026-10-12" and due[0]["minimum_cents"] == 10300


@pytest.mark.parametrize("format", ["json", "csv"])
def test_snapshot_export_import_roundtrip_and_backup(planner, tmp_path, format):
    loan(planner)
    planner.pay("D-1", "100", "2026-09-15", idempotency_key="p1")
    planner.set_budget("2026-12", "900")
    simulate(planner, "snowball", "100", start="2026-10", save=True)
    path = tmp_path / f"snapshot.{format}"
    assert planner.export(path, format)["count"] == 4
    if format == "csv":
        assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    restored = Planner(tmp_path / "restored")
    assert restored.import_file(path) == {"inserted": 4, "skipped": 0}
    assert restored.import_file(path) == {"inserted": 0, "skipped": 4}
    assert restored.list_debts()[0]["balance_cents"] == 91000
    backup = planner.backup()
    with zipfile.ZipFile(backup["path"]) as archive:
        assert set(archive.namelist()) == {"debts.db", "settings.json"}
        archive.extract("debts.db", tmp_path / "backup")
    with sqlite3.connect(tmp_path / "backup/debts.db") as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT sum(principal_cents) FROM payments").fetchone()[0] == 9000
    with pytest.raises(ValueError):
        planner.export(planner.path)


def test_invalid_import_rolls_back_and_refuses_journal_mismatch(planner, tmp_path):
    loan(planner)
    planner.pay("D-1", "100", "2026-09-15")
    path = tmp_path / "snapshot.json"
    planner.export(path)
    rows = json.loads(path.read_text())
    rows[0]["balance_cents"] -= 1
    path.write_text(json.dumps(rows))
    restored = Planner(tmp_path / "restored")
    with pytest.raises(ValueError, match="disagrees"):
        restored.import_file(path)
    assert restored.list_debts() == []


def test_five_debt_comparison_snapshot_and_charts(planner, examples, tmp_path):
    assert planner.import_file(examples / "five_debts.json") == {"inserted": 6, "skipped": 0}
    assert planner.import_file(examples / "five_debts.json") == {"inserted": 0, "skipped": 6}
    expected = json.loads((examples / "expected_comparison.json").read_text())
    actual = compare(planner, "600", "2026-10")
    assert summary(actual) == expected
    generated = report(planner, "600", "2026-10", out=tmp_path / "report.md")
    assert generated["summary"] == expected
    assert len(generated["charts"]) == 2
    assert "Avalanche monthly plan" in (tmp_path / "report.md").read_text()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"type": "invalid"},
        {"type": "equal_installment"},
        {"due_day": 32},
        {"annual_rate": "NaN"},
        {"minimum": "-1"},
        {"fee_rate": "1%"},
        {"type": "interest_only"},
    ],
)
def test_invalid_debt_data(planner, kwargs):
    arguments = {
        "code": "INVALID",
        "type": "personal",
        "balance": "100",
        "start_date": "2026-10-01",
        **kwargs,
    }
    with pytest.raises(ValueError):
        planner.add(**arguments)


def test_early_extra_payment_can_exhaust_other_minimums(planner, examples):
    planner.import_file(examples / "five_debts.json")
    planner.pay("PERSONAL-E", "100", "2026-10-10", interest="0")
    result = simulate(planner, "avalanche", "600", start="2026-10")
    assert result["failure"]["code"] == "MINIMUM_BUDGET_SHORTFALL"
    assert result["failure"]["required_cents"] == 54715
    assert result["failure"]["available_cents"] == 50000
    assert result["failure"]["shortfall_cents"] == 4715
    assert simulate(planner, "avalanche", "700", start="2026-10")["complete"]
