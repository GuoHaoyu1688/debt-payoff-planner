from decimal import Decimal

import pytest

from debt_payoff_planner.core.calculations import daily_apr, fee_apr, installment
from debt_payoff_planner.core.simulation import compare, extra, simulate


def add(planner, kind, value="1200", **kwargs):
    return planner.add("D-1", kind, value, start_date="2026-10-01", due_day=15, **kwargs)


def test_manual_fixed_installment_formula_and_rounding(planner):
    # P=1,200; r=1%; n=2. A=1200*.01*1.0201/.0201=609.014925...
    assert installment(120000, "12%", 2) == 60901
    add(planner, "equal_installment", annual_rate="12%", term_months=2)
    result = simulate(planner, "baseline", "700", start="2026-10")
    assert [r["payment_cents"] for r in result["rows"]] == [
        60901,
        60902,
    ]  # Final payment clears the one-cent rounding residual.
    assert [r["interest_cents"] for r in result["rows"]] == [1200, 603]
    assert result["total_interest_cents"] == 1803
    assert result["complete"] and result["payoff_month"] == "2026-11"


def test_manual_equal_principal(planner):
    add(planner, "equal_principal", annual_rate="12%", term_months=3)
    result = simulate(planner, "baseline", "500", start="2026-10")
    assert [r["payment_cents"] for r in result["rows"]] == [41200, 40800, 40400]
    assert [r["principal_cents"] for r in result["rows"]] == [40000, 40000, 40000]
    assert result["total_interest_cents"] == 2400


def test_manual_interest_only_balloon(planner):
    add(planner, "interest_only", annual_rate="12%", term_months=3)
    planner.set_budget("2026-12", "1212")
    result = simulate(planner, "baseline", "12", start="2026-10")
    assert [r["payment_cents"] for r in result["rows"]] == [1200, 1200, 121200]
    assert result["total_interest_cents"] == 3600
    planner.set_budget("2026-12", "100")
    failed = simulate(planner, "baseline", "12", start="2026-10")
    assert not failed["complete"] and failed["failure"]["shortfall_cents"] == 111200


def test_manual_card_minimum_and_explicit_grace(planner):
    add(
        planner,
        "credit_card",
        value="1000",
        annual_rate="24%",
        minimum="50",
        minimum_ratio="10%",
        grace_until="2026-10-31",
    )
    result = simulate(planner, "baseline", "200", start="2026-10", months=2)
    assert result["rows"][0]["interest_cents"] == 0
    assert result["rows"][0]["payment_cents"] == 10000
    # November: opening 900; interest 18; minimum 10% of 918 = 91.80.
    assert result["rows"][1]["interest_cents"] == 1800
    assert result["rows"][1]["payment_cents"] == 9180
    assert result["rows"][1]["balance_cents"] == 82620


def test_manual_fee_installment_retention_and_waiver(planner):
    debt = add(planner, "fee_installment", fee_rate="0.6%", term_months=12)
    result = simulate(planner, "baseline", "200", start="2026-10")
    assert all(r["payment_cents"] == 10720 for r in result["rows"])
    assert result["total_interest_cents"] == 8640
    accelerated = simulate(planner, "avalanche", "1300", start="2026-10")
    assert accelerated["months"] == 1 and accelerated["total_paid_cents"] == 128640
    assert (
        accelerated["total_interest_cents"] == 8640
    )  # Retained fees are not imaginary interest savings.
    planner.update(debt["id"], {"fee_policy": "waive"})
    waived = simulate(planner, "avalanche", "1300", start="2026-10")
    assert waived["total_paid_cents"] == 120720 and waived["total_interest_cents"] == 720


def test_retained_fees_survive_zero_principal(planner):
    add(planner, "fee_installment", value="100", fee_rate="1%", term_months=3)
    result = simulate(planner, "avalanche", "101", start="2026-10")
    assert result["rows"][0]["balance_cents"] == 0
    assert result["rows"][0]["total_outstanding_cents"] == 200
    assert result["total_paid_cents"] == 10300 and result["complete"]


def test_manual_personal_interest_free_and_budget_overrides(planner):
    add(planner, "personal", value="900", minimum="100")
    planner.set_budget("2026-11", "500")
    result = simulate(planner, "snowball", "200", start="2026-10")
    assert [r["payment_cents"] for r in result["rows"]] == [20000, 50000, 20000]
    assert result["total_interest_cents"] == 0


def test_manual_irr_one_period_and_npv_cross_check():
    one = fee_apr("1%", 1)
    assert Decimal(one["monthly_irr"]) == Decimal("0.01")
    assert Decimal(one["nominal_annualized_irr"]) == Decimal("0.12")
    assert abs(Decimal(one["effective_annual_rate"]) - ((Decimal("1.01") ** 12) - 1)) < Decimal(
        "1e-12"
    )
    result = fee_apr("0.6%", 12)
    rate = Decimal(result["monthly_irr"])
    payment = Decimal(1) / 12 + Decimal("0.006")
    assert abs(sum(payment / (1 + rate) ** k for k in range(1, 13)) - 1) < Decimal("1e-10")
    assert Decimal("0.13") < Decimal(result["nominal_annualized_irr"]) < Decimal("0.131")
    assert fee_apr("0", 12)["monthly_irr"] == "0.000000000000"
    upfront = fee_apr("10%", 1, "upfront")
    assert abs(Decimal(upfront["monthly_irr"]) - Decimal(1) / 9) < Decimal("1e-12")
    total = fee_apr("12%", 12, "total")
    monthly = fee_apr("1%", 12)
    assert total["monthly_irr"] == monthly["monthly_irr"]


def test_daily_rate_conversion_is_not_effective_yield(planner):
    result = daily_apr("0.05%")
    assert Decimal(result["nominal_annual_rate"]) == Decimal("0.1825")
    assert Decimal(result["effective_annual_rate"]) > Decimal("0.20")
    debt = add(planner, "personal", daily_rate="0.05%", minimum="100")
    assert Decimal(debt["annual_rate"]) == Decimal("0.1825")


def test_strategy_order_and_lump_sum_best_target(planner):
    planner.add(
        "SMALL",
        "personal",
        "100",
        annual_rate="0",
        minimum="10",
        start_date="2026-10-01",
        priority=1,
    )
    planner.add(
        "COSTLY",
        "personal",
        "1000",
        annual_rate="24%",
        minimum="30",
        start_date="2026-10-01",
        priority=2,
    )
    comparison = compare(planner, "200", start="2026-10")
    snow = comparison["strategies"]["snowball"]
    avalanche = comparison["strategies"]["avalanche"]
    assert next(a for a in snow["rows"][0]["debts"] if a["code"] == "SMALL")["balance_cents"] == 0
    assert avalanche["total_interest_cents"] < snow["total_interest_cents"]
    custom = simulate(planner, "custom", "200", start="2026-10", order=["SMALL", "COSTLY"])
    assert custom["total_interest_cents"] == snow["total_interest_cents"]
    lump = extra(planner, "100", "2026-10", "200", "2026-10")
    assert lump["lowest_interest_target"] == "COSTLY"
    assert all(r["interest_saved_cents"] is not None for r in lump["candidates"])


def test_infeasible_and_zero_progress_are_bounded(planner):
    add(planner, "personal", minimum="100")
    failed = simulate(planner, "avalanche", "99", start="2026-10")
    assert failed["failure"]["code"] == "MINIMUM_BUDGET_SHORTFALL" and failed["months"] == 0
    planner.update("D-1", {"minimum": "0"})
    result = simulate(planner, "baseline", "0", start="2026-10", months=4)
    assert result["failure"]["code"] == "HORIZON_EXCEEDED" and len(result["rows"]) == 4
    comp = compare(planner, "100", start="2026-10", months=24)
    assert comp["strategies"]["snowball"]["interest_saved_cents"] is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"strategy": "bad"},
        {"months": 0},
        {"order": ["UNKNOWN"], "strategy": "custom"},
        {"extra_amount": "10", "extra_month": "2026-09", "extra_target": "D-1"},
    ],
)
def test_simulation_validation(planner, kwargs):
    add(planner, "personal")
    with pytest.raises(ValueError):
        simulate(planner, start="2026-10", **kwargs)
