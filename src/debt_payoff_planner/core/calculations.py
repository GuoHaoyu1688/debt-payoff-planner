"""Decimal loan formulas and explicitly defined fee cash-flow yields."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext
from typing import Any

from .money import add_months, decimal, rounded

TYPES = {
    "credit_card",
    "equal_installment",
    "equal_principal",
    "interest_only",
    "fee_installment",
    "personal",
}


def parse_rate(value: Any, maximum: str = "10") -> Decimal:
    raw = str(value).strip()
    result = decimal(raw[:-1]) / 100 if raw.endswith("%") else decimal(value)
    if not 0 <= result <= decimal(maximum):
        raise ValueError(
            f"rate must be between 0 and {maximum}; use 0.12 or 12% for twelve percent"
        )
    return result


def installment(principal: int, annual_rate: str | Decimal, months: int) -> int:
    """Fixed principal-and-interest payment P*r/(1-(1+r)^(-n))."""
    if type(months) is not int or not 1 <= months <= 1200:
        raise ValueError("term months must be an integer from 1 to 1200")
    r = parse_rate(annual_rate) / 12
    with localcontext() as ctx:
        ctx.prec = 40
        return rounded(
            Decimal(principal) / months
            if not r
            else Decimal(principal) * r / (1 - (1 + r) ** -months)
        )


def fee_apr(fee_rate: str, months: int, mode: str = "monthly") -> dict[str, Any]:
    """IRR of a unit principal and equal month-end installments, using bisection."""
    fee = parse_rate(fee_rate, "1")
    if type(months) is not int or not 1 <= months <= 1200:
        raise ValueError("months must be an integer from 1 to 1200")
    if mode not in {"monthly", "total", "upfront"}:
        raise ValueError("fee mode must be monthly, total or upfront")
    if mode == "upfront" and fee >= 1:
        raise ValueError("upfront fee must be below the amount advanced")
    with localcontext() as ctx:
        ctx.prec = 40
        initial = 1 - fee if mode == "upfront" else Decimal(1)
        payment = Decimal(1) / months + (
            fee if mode == "monthly" else fee / months if mode == "total" else 0
        )

        def present_value(r: Decimal) -> Decimal:
            return payment * months if not r else payment * (1 - (1 + r) ** -months) / r

        if fee == 0:
            monthly = Decimal(0)
        else:
            low, high = Decimal(0), Decimal(1)
            for _ in range(32):
                if present_value(high) <= initial:
                    break
                high *= 2
            else:
                raise ValueError("fee IRR is outside the supported range")
            for _ in range(180):
                middle = (low + high) / 2
                if present_value(middle) > initial:
                    low = middle
                else:
                    high = middle
            monthly = (low + high) / 2
        return {
            "fee_rate": str(fee),
            "months": months,
            "fee_mode": mode,
            "monthly_irr": format(monthly, ".12f"),
            "nominal_annualized_irr": format(monthly * 12, ".12f"),
            "effective_annual_rate": format((1 + monthly) ** 12 - 1, ".12f"),
            "total_fee_fraction_of_original_principal": str(
                fee * months if mode == "monthly" else fee
            ),
            "assumption": "Equal month-end payments and one initial advance; planning yield, not a jurisdiction-specific APR disclosure.",
        }


def daily_apr(daily_rate: str) -> dict[str, Any]:
    daily = parse_rate(daily_rate, "1")
    return {
        "daily_rate": str(daily),
        "nominal_annual_rate": str(daily * 365),
        "effective_annual_rate": format((1 + daily) ** 365 - 1, ".12f"),
        "assumption": "365-day year; effective annual rate assumes daily compounding. Simulator uses nominal annual / 12.",
    }


def month_distance(first: str, last: str) -> int:
    a, b = date.fromisoformat(first[:7] + "-01"), date.fromisoformat(last[:7] + "-01")
    return (b.year - a.year) * 12 + b.month - a.month


def due_date(debt: dict[str, Any], month: str) -> date:
    return add_months(date(2000, 1, debt["due_day"]), month_distance("2000-01", month))


def first_due(debt: dict[str, Any]) -> date:
    start = date.fromisoformat(debt["start_date"])
    candidate = due_date(debt, start.strftime("%Y-%m"))
    return (
        candidate
        if candidate >= start
        else due_date(debt, add_months(start.replace(day=1), 1).strftime("%Y-%m"))
    )


def monthly_charge(debt: dict[str, Any], balance: int, month: str) -> int:
    if month < first_due(debt).strftime("%Y-%m"):
        return 0
    if debt["type"] == "fee_installment":
        return (
            rounded(decimal(debt["opening_balance_cents"]) * decimal(debt["fee_rate"]))
            if month <= debt["maturity_date"][:7]
            else 0
        )
    if balance <= 0:
        return 0
    if (
        debt["type"] == "credit_card"
        and debt.get("grace_until")
        and due_date(debt, month).isoformat() <= debt["grace_until"]
    ):
        return 0
    return rounded(decimal(balance) * decimal(debt["annual_rate"]) / 12)


def remaining_fees(debt: dict[str, Any], month: str) -> int:
    if debt["type"] != "fee_installment" or debt["fee_policy"] == "waive":
        return 0
    return max(0, month_distance(month, debt["maturity_date"])) * rounded(
        decimal(debt["opening_balance_cents"]) * decimal(debt["fee_rate"])
    )


def minimum_due(debt: dict[str, Any], balance: int, charges: int, month: str) -> int:
    if balance + charges <= 0 or month < first_due(debt).strftime("%Y-%m"):
        return 0
    if debt["maturity_date"] and month >= debt["maturity_date"][:7]:
        return balance + charges
    kind = debt["type"]
    if kind == "equal_installment":
        required = debt["scheduled_payment_cents"]
    elif kind in {"equal_principal", "fee_installment"}:
        required = debt["principal_step_cents"] + charges
    elif kind == "interest_only":
        required = charges
    elif kind == "credit_card":
        required = rounded(decimal(balance + charges) * decimal(debt["minimum_ratio"]))
    else:
        required = 0
    return min(balance + charges, max(required, debt["minimum_cents"]))
