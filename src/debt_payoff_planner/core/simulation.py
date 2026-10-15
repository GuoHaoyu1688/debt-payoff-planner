"""Bounded monthly repayment simulation and saved-plan progress comparison."""

from __future__ import annotations

from datetime import date
from typing import Any

from .calculations import due_date, fee_apr, first_due, minimum_due
from .money import add_months, cents, decimal, month_date
from .store import NotFound, Planner

STRATEGIES = {"snowball", "avalanche", "custom", "baseline"}
ASSUMPTIONS = [
    "Current recorded balances are the opening snapshot; this is not a reconstruction of past balances.",
    "Interest uses nominal annual rate / 12, rounded HALF_UP to cents each month; daily rates convert using 365 days.",
    "No new borrowing, late fees, penalty APR changes, insurance or taxes. Unpaid actual charges are tracked separately without compounding.",
    "Every minimum is paid first. Extra funds follow the selected priority. Unused monthly budget is not carried forward.",
    "Stored monthly budgets replace the default budget. Actual payments already recorded in a simulated month consume that month's budget.",
    "Fixed installments and equal-principal steps retain their original amounts; prepayments shorten repayment rather than recasting automatically.",
    "Maturity requires full remaining principal and assessed charges in its month, including interest-only balloon payments.",
    "Credit-card grace is explicit until grace_until, with no retroactive interest or new purchases; verify actual statement charges.",
    "Flat installment fees use original principal each month. Retain keeps future fees payable even after principal is zero; waive cancels unassessed fees on principal payoff.",
    "Charges already assessed by an actual payment are not assessed twice in the same month. Interest overrides represent newly assessed statement charges.",
]


def outstanding(row: dict[str, Any]) -> int:
    return row["balance_cents"] + row["unpaid_interest_cents"] + row["future_fees_cents"]


def simulate(
    store: Planner,
    strategy: str = "avalanche",
    budget: str | None = None,
    start: str | None = None,
    months: int = 600,
    order: list[str] | None = None,
    save: bool = False,
    extra_amount: str | None = None,
    extra_month: str | None = None,
    extra_target: str | None = None,
) -> dict[str, Any]:
    if strategy not in STRATEGIES:
        raise ValueError("strategy must be snowball, avalanche, custom or baseline")
    if type(months) is not int or not 1 <= months <= 1200:
        raise ValueError("months must be an integer from 1 to 1200")
    raw_budget = budget if budget is not None else store.default_budget
    if raw_budget is None:
        raise ValueError(
            "supply --budget or configure default_budget; monthly overrides alone do not define other months"
        )
    default_budget = cents(raw_budget)
    first = month_date(start) if start else store.today().replace(day=1)
    states = {r["id"]: dict(r) for r in store.list_debts()}
    original = {key: dict(value) for key, value in states.items()}
    if any(
        r["last_payment_date"] and r["last_payment_date"][:7] > first.strftime("%Y-%m")
        for r in states.values()
    ):
        raise ValueError("simulation cannot start before a month with recorded actual payments")
    by_code = {r["code"]: r["id"] for r in states.values()}
    if order is not None and (
        len(set(order)) != len(order) or any(code not in by_code for code in order)
    ):
        raise ValueError("custom order contains duplicate or unknown codes")
    if strategy == "custom" and order is not None and set(order) != set(by_code):
        raise ValueError("explicit custom order must include every debt code exactly once")
    ranks = {by_code[code]: index for index, code in enumerate(order or [])}
    effective_rates = {
        r["id"]: decimal(fee_apr(r["fee_rate"], r["term_months"])["nominal_annualized_irr"])
        if r["type"] == "fee_installment"
        else decimal(r["annual_rate"])
        for r in states.values()
    }
    extra = cents(extra_amount) if extra_amount is not None else 0
    if extra and (not extra_month or extra_target not in by_code):
        raise ValueError("an extra payment requires a valid month and target debt code")
    if extra_month and month_date(extra_month) < first:
        raise ValueError("extra month cannot precede simulation start")
    budgets = store.budgets()
    actual = store.payments()
    rows, warnings = [], []
    payoff_dates: dict[str, str | None] = {
        r["code"]: r["last_payment_date"] if outstanding(r) == 0 else None for r in states.values()
    }
    total_interest = total_principal = total_paid = 0
    failure = None
    extra_applied = 0
    extra_eligible = False
    for index in range(months):
        if all(outstanding(row) == 0 for row in states.values()):
            break
        month = add_months(first, index).strftime("%Y-%m")
        actual_month = [p for p in actual if p["date"][:7] == month]
        actual_paid = sum(p["amount_cents"] for p in actual_month)
        monthly_budget = budgets.get(month, default_budget)
        extra_this_month = extra if month == extra_month else 0
        available = monthly_budget + extra_this_month - actual_paid
        allocations = {}
        eligible = []
        for key, row in states.items():
            if outstanding(row) <= 0 or month < first_due(row).strftime("%Y-%m"):
                continue
            eligible.append(key)
            new_charge, fees = store.accrue(row, month)
            row["unpaid_interest_cents"] += new_charge
            row["future_fees_cents"] = fees
            row["accrued_through"] = month
            required = minimum_due(row, row["balance_cents"], row["unpaid_interest_cents"], month)
            already = [p for p in actual_month if p["debt_id"] == key]
            if already:
                import json

                before = json.loads(already[0]["before_json"])
                charge, _ = store.accrue(before, month)
                required = min(
                    outstanding(row),
                    max(
                        0,
                        minimum_due(
                            before,
                            before["balance_cents"],
                            before["unpaid_interest_cents"] + charge,
                            month,
                        )
                        - sum(p["amount_cents"] for p in already),
                    ),
                )
            if row["maturity_date"] and month >= row["maturity_date"][:7]:
                required = outstanding(row)
            allocations[key] = {
                "id": key,
                "code": row["code"],
                "date": min(
                    due_date(row, month), date.fromisoformat(row["maturity_date"])
                ).isoformat()
                if row["maturity_date"] and row["maturity_date"][:7] == month
                else due_date(row, month).isoformat(),
                "required_cents": required,
                "payment_cents": 0,
                "principal_cents": 0,
                "interest_cents": 0,
                "new_charge_cents": new_charge,
            }
        required_total = sum(a["required_cents"] for a in allocations.values())
        if available < required_total:
            failure = {
                "code": "MINIMUM_BUDGET_SHORTFALL",
                "month": month,
                "required_cents": required_total,
                "available_cents": max(0, available),
                "shortfall_cents": required_total - available,
                "actual_already_paid_cents": actual_paid,
            }
            warnings.append(
                f"{month}: budget cannot cover required minimums and maturity balloons; simulation stopped before allocating this month"
            )
            break

        def pay(key: str, requested: int) -> int:
            nonlocal available, total_interest, total_principal, total_paid
            row = states[key]
            maximum = (
                outstanding(row)
                if row["fee_policy"] == "retain"
                else row["balance_cents"] + row["unpaid_interest_cents"]
            )
            value = min(requested, maximum, available)
            interest = min(value, row["unpaid_interest_cents"])
            principal = min(value - interest, row["balance_cents"])
            fee_prepaid = value - interest - principal
            row["unpaid_interest_cents"] -= interest
            row["balance_cents"] -= principal
            row["future_fees_cents"] -= fee_prepaid
            if row["balance_cents"] == 0 and row["fee_policy"] == "waive":
                row["future_fees_cents"] = 0
            available -= value
            total_paid += value
            total_interest += interest + fee_prepaid
            total_principal += principal
            allocation = allocations[key]
            allocation["payment_cents"] += value
            allocation["principal_cents"] += principal
            allocation["interest_cents"] += interest + fee_prepaid
            if outstanding(row) == 0:
                payoff_dates[row["code"]] = allocation["date"]
            return value

        for key in eligible:
            pay(key, allocations[key]["required_cents"])
        if extra_this_month:
            target = by_code[extra_target]
            if target in eligible and outstanding(states[target]) > 0:
                extra_eligible = True
                extra_applied = pay(target, min(extra_this_month, available))
            else:
                warnings.append(
                    f"Extra target {extra_target} has no remaining payable obligation after this month's minimums"
                )
        if strategy != "baseline":
            if strategy == "snowball":

                def priority(key):
                    return (outstanding(states[key]), states[key]["code"])
            elif strategy == "avalanche":

                def priority(key):
                    return (
                        -effective_rates[key],
                        outstanding(states[key]),
                        states[key]["code"],
                    )
            else:

                def priority(key):
                    return (
                        ranks.get(key, states[key]["priority"]),
                        states[key]["code"],
                    )

            for key in sorted(eligible, key=priority):
                pay(key, available)
        for key, a in allocations.items():
            a["balance_cents"] = states[key]["balance_cents"]
            a["remaining_charges_cents"] = (
                states[key]["unpaid_interest_cents"] + states[key]["future_fees_cents"]
            )
        rows.append(
            {
                "month": month,
                "budget_cents": monthly_budget,
                "extra_budget_cents": extra_this_month,
                "actual_paid_cents": actual_paid,
                "payment_cents": sum(a["payment_cents"] for a in allocations.values()),
                "interest_cents": sum(a["interest_cents"] for a in allocations.values()),
                "principal_cents": sum(a["principal_cents"] for a in allocations.values()),
                "unused_budget_cents": available,
                "balance_cents": sum(r["balance_cents"] for r in states.values()),
                "total_outstanding_cents": sum(outstanding(r) for r in states.values()),
                "debts": list(allocations.values()),
                "balances_by_code": {r["code"]: outstanding(r) for r in states.values()},
            }
        )
    complete = all(outstanding(r) == 0 for r in states.values())
    if not complete and failure is None:
        failure = {
            "code": "HORIZON_EXCEEDED",
            "months": months,
            "remaining_cents": sum(outstanding(r) for r in states.values()),
        }
        warnings.append(
            "No complete payoff within the bounded horizon; increase budget or review minimums and terms"
        )
    result = {
        "strategy": strategy,
        "start": first.strftime("%Y-%m"),
        "budget_cents": default_budget,
        "currency": store.currency,
        "complete": complete,
        "payoff_month": rows[-1]["month"] if complete and rows else None,
        "months": len(rows),
        "total_interest_cents": total_interest,
        "total_principal_cents": total_principal,
        "total_paid_cents": total_paid,
        "remaining_cents": sum(outstanding(r) for r in states.values()),
        "debt_payoff_dates": payoff_dates,
        "failure": failure,
        "warnings": warnings,
        "rows": rows,
        "initial_debts": list(original.values()),
        "assumptions": ASSUMPTIONS,
        "extra_target_eligible": extra_eligible,
        "extra_applied_to_target_cents": extra_applied,
    }
    if save:
        result["plan_id"] = store.save_plan(result)
    return result


def compare(
    store: Planner,
    budget: str | None = None,
    start: str | None = None,
    months: int = 600,
    order: list[str] | None = None,
) -> dict[str, Any]:
    results = {
        strategy: simulate(store, strategy, budget, start, months, order)
        for strategy in ("baseline", "snowball", "avalanche", "custom")
    }
    baseline = results["baseline"]
    for result in results.values():
        result["interest_saved_cents"] = (
            baseline["total_interest_cents"] - result["total_interest_cents"]
            if baseline["complete"] and result["complete"]
            else None
        )
    feasible = [r for r in results.values() if r["complete"]]
    return {
        "strategies": results,
        "lowest_interest_strategy": min(
            feasible, key=lambda r: (r["total_interest_cents"], r["months"])
        )["strategy"]
        if feasible
        else None,
        "warnings": []
        if baseline["complete"]
        else ["Baseline is incomplete; interest savings are unavailable, not zero."],
        "currency": store.currency,
    }


def extra(
    store: Planner,
    amount: str,
    month: str,
    budget: str | None = None,
    start: str | None = None,
    strategy: str = "avalanche",
    months: int = 600,
) -> dict[str, Any]:
    if cents(amount) <= 0:
        raise ValueError("extra amount must be positive")
    month_date(month)
    base = simulate(store, strategy, budget, start, months)
    candidates = []
    for debt in store.list_debts():
        result = simulate(
            store,
            strategy,
            budget,
            start,
            months,
            extra_amount=amount,
            extra_month=month,
            extra_target=debt["code"],
        )
        candidates.append(
            {
                "code": debt["code"],
                "eligible": result["extra_target_eligible"],
                "complete": result["complete"],
                "payoff_month": result["payoff_month"],
                "total_interest_cents": result["total_interest_cents"],
                "interest_saved_cents": base["total_interest_cents"]
                - result["total_interest_cents"]
                if base["complete"] and result["complete"]
                else None,
                "applied_to_target_cents": result["extra_applied_to_target_cents"],
                "failure": result["failure"],
            }
        )
    feasible = [r for r in candidates if r["eligible"] and r["complete"]]
    return {
        "month": month,
        "amount_cents": cents(amount),
        "strategy": strategy,
        "candidates": candidates,
        "lowest_interest_target": min(
            feasible, key=lambda r: (r["total_interest_cents"], r["payoff_month"], r["code"])
        )["code"]
        if feasible
        else None,
        "warnings": [
            "The extra budget covers minimums first, then targets the selected debt; any surplus follows the strategy. Feasibility depends on the stated fee policy."
        ],
        "currency": store.currency,
    }


def progress(store: Planner, plan_id: str, as_of: str | None = None) -> dict[str, Any]:
    plan = store.plans(plan_id)
    cutoff = date.fromisoformat(as_of) if as_of else store.today()
    if cutoff > store.today() or cutoff < month_date(plan["start"]):
        raise ValueError("progress as_of must be between the saved plan start and today")
    debts = {r["id"]: r for r in store.list_debts()}
    results = []
    for original in plan["initial_debts"]:
        current = debts.get(original["id"])
        if current is None:
            raise NotFound("a debt from the saved plan is missing")
        payments = [p for p in store.payments() if p["debt_id"] == current["id"]]
        future = [p for p in payments if p["date"] > cutoff.isoformat()]
        balance = (
            json_snapshot(future[0]["before_json"])["balance_cents"]
            if future
            else current["balance_cents"]
        )
        trajectory = [a for row in plan["rows"] for a in row["debts"] if a["id"] == current["id"]]
        expected = original["balance_cents"]
        for row in trajectory:
            if row["date"] <= cutoff.isoformat():
                expected = row["balance_cents"]
        matching = next(
            (row["date"] for row in trajectory if row["balance_cents"] <= balance), None
        )
        actual_payoff = (
            current["last_payment_date"] if outstanding(current) == 0 and not future else None
        )
        planned_payoff = plan["debt_payoff_dates"].get(current["code"])
        if actual_payoff and planned_payoff:
            difference = (
                date.fromisoformat(planned_payoff) - date.fromisoformat(actual_payoff)
            ).days
        elif balance == expected:
            difference = 0
        elif matching:
            difference = (date.fromisoformat(matching) - cutoff).days
        else:
            difference = 0
        results.append(
            {
                "code": current["code"],
                "actual_principal_cents": balance,
                "planned_principal_cents": expected,
                "principal_difference_cents": expected - balance,
                "status": "ahead" if difference > 0 else "behind" if difference < 0 else "on_track",
                "days": abs(difference),
                "planned_payoff_date": planned_payoff,
                "actual_payoff_date": actual_payoff,
                "principal_paid_percent": format(
                    decimal(original["opening_balance_cents"] - balance)
                    / original["opening_balance_cents"]
                    * 100,
                    ".2f",
                ),
            }
        )
    return {
        "plan_id": plan_id,
        "as_of": cutoff.isoformat(),
        "debts": results,
        "assumption": "Lead/lag uses the first scheduled principal milestone at or below actual principal; exact payoff dates when both are available. It is not a lender delinquency calculation.",
    }


def json_snapshot(text: str) -> dict[str, Any]:
    import json

    return json.loads(text)
