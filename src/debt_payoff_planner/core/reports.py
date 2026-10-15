"""Offline comparison reports with local balance and stacked-debt charts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .money import amount
from .simulation import compare
from .store import Planner


def summary(comparison: dict[str, Any]) -> dict[str, Any]:
    return {
        name: {
            key: result[key]
            for key in (
                "complete",
                "payoff_month",
                "months",
                "total_interest_cents",
                "total_paid_cents",
                "interest_saved_cents",
                "debt_payoff_dates",
                "failure",
            )
        }
        for name, result in comparison["strategies"].items()
    }


def markdown(comparison: dict[str, Any]) -> str:
    currency = comparison["currency"]
    results = comparison["strategies"]
    lines = [
        "# Debt payoff comparison",
        "",
        f"Currency: {currency}. Figures are planning estimates from the recorded opening balances.",
        "",
        "| Strategy | Complete | Payoff month | Finance charges | Saved vs baseline |",
        "| --- | --- | --- | ---: | ---: |",
    ]
    for name, row in results.items():
        savings = (
            amount(row["interest_saved_cents"])
            if row["interest_saved_cents"] is not None
            else "Unavailable"
        )
        lines.append(
            f"| {name} | {row['complete']} | {row['payoff_month'] or 'Not reached'} | {amount(row['total_interest_cents'])} | {savings} |"
        )
        if row["failure"]:
            lines.extend(["", f"{name}: {row['failure']}", ""])
    lines.extend(
        [
            "",
            "Finance charges include interest and flat installment fees. Incomplete paths report charges paid so far, not a complete lifetime cost.",
            "",
            "## Per-debt payoff dates",
            "",
            "| Debt | Baseline | Snowball | Avalanche | Custom |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for code in results["avalanche"]["debt_payoff_dates"]:
        lines.append(
            "| "
            + " | ".join(
                [code]
                + [
                    str(results[s]["debt_payoff_dates"].get(code) or "Not reached")
                    for s in ("baseline", "snowball", "avalanche", "custom")
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Avalanche monthly plan",
            "",
            "| Month | Budget | Actual already paid | Projected payment | Finance charges | Remaining obligation |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in results["avalanche"]["rows"]:
        lines.append(
            "| "
            + " | ".join(
                [row["month"]]
                + [
                    amount(row[key])
                    for key in (
                        "budget_cents",
                        "actual_paid_cents",
                        "payment_cents",
                        "interest_cents",
                        "total_outstanding_cents",
                    )
                ]
            )
            + " |"
        )
    lines.extend(
        ["", "## Assumptions and limitations", ""]
        + ["- " + s for s in results["avalanche"]["assumptions"] + comparison["warnings"]]
    )
    lines.extend(
        [
            "",
            "This tool does not make payments or recommend borrowing products. It is not financial advice. Verify contractual minimums, fees, grace rules and balances against lender statements.",
            "",
        ]
    )
    return "\n".join(lines)


def report(
    store: Planner,
    budget: str | None = None,
    start: str | None = None,
    months: int = 600,
    out: str | Path | None = None,
) -> dict[str, Any]:
    data = compare(store, budget, start, months)
    first = data["strategies"]["avalanche"]["start"]
    path = (
        Path(out).expanduser().resolve()
        if out
        else store.data_dir / "reports" / f"comparison_{first}.md"
    )
    store._guard_output(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figures = []
    paths = []
    try:
        fig, ax = plt.subplots(figsize=(10, 4.5), layout="constrained")
        figures.append(fig)
        for name, result in data["strategies"].items():
            values = [
                sum(
                    d["balance_cents"] + d["future_fees_cents"] + d["unpaid_interest_cents"]
                    for d in result["initial_debts"]
                )
            ] + [r["total_outstanding_cents"] for r in result["rows"]]
            ax.plot(range(len(values)), [v / 100 for v in values], label=name)
        ax.set(
            title="Remaining obligation by strategy",
            xlabel="Months from opening snapshot",
            ylabel=store.currency,
        )
        ax.legend()
        ax.grid(alpha=0.2)
        line = path.with_name(path.stem + "-balances.png")
        fig.savefig(line, dpi=150)
        paths.append(str(line))
        fig, ax = plt.subplots(figsize=(10, 4.5), layout="constrained")
        figures.append(fig)
        avalanche = data["strategies"]["avalanche"]
        codes = [d["code"] for d in avalanche["initial_debts"]]
        values = [
            [d["balance_cents"] + d["future_fees_cents"] + d["unpaid_interest_cents"]]
            + [r["balances_by_code"][d["code"]] for r in avalanche["rows"]]
            for d in avalanche["initial_debts"]
        ]
        if codes:
            ax.stackplot(
                range(len(values[0])),
                *[[v / 100 for v in series] for series in values],
                labels=codes,
                alpha=0.85,
            )
            ax.legend(loc="upper right")
        ax.set(
            title="Avalanche: remaining obligation by debt",
            xlabel="Months from opening snapshot",
            ylabel=store.currency,
        )
        stack = path.with_name(path.stem + "-stacked.png")
        fig.savefig(stack, dpi=150)
        paths.append(str(stack))
        path.write_text(
            markdown(data)
            + "\n## Charts\n\n"
            + "\n\n".join(
                f"![{label}]({Path(p).name})"
                for p, label in zip(
                    paths, ["Strategy balance curves", "Debt balances under avalanche"], strict=True
                )
            )
            + "\n",
            encoding="utf-8",
        )
        return {
            "markdown": str(path),
            "charts": paths,
            "summary": summary(data),
            "warnings": data["warnings"],
        }
    finally:
        for fig in figures:
            plt.close(fig)
