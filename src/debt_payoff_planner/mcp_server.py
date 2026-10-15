"""Official MCP stdio tools sharing the planner core."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import make_planner
from .core.simulation import compare, simulate
from .core.store import Planner


def create_server(planner: Planner | None = None) -> FastMCP:
    store = planner or make_planner()
    server = FastMCP("debt-payoff-planner")

    @server.tool()
    def list_debts() -> list[dict[str, Any]]:
        """Read current principal, assessed charges, retained fees and contract assumptions. Amounts are integer cents."""
        return store.list_debts()

    @server.tool()
    def simulate_plan(
        strategy: str,
        budget: str,
        start: str | None = None,
        months: int = 600,
        order: list[str] | None = None,
    ) -> dict[str, Any]:
        """Read a bounded monthly payoff projection. Budget is major currency units; strategies: snowball, avalanche, custom, baseline. Does not write or pay money."""
        return simulate(store, strategy, budget, start, months, order)

    @server.tool()
    def compare_strategies(
        budget: str, start: str | None = None, months: int = 600
    ) -> dict[str, Any]:
        """Compare four strategies without writing. Incomplete baselines produce null interest savings, never fabricated savings."""
        return compare(store, budget, start, months)

    @server.tool()
    def record_payment(
        debt_id: str,
        amount: str,
        date: str,
        interest: str | None = None,
        note: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Write an actual payment journal entry and reduce local principal. No external money transfer. Optional interest replaces estimated newly assessed finance charges; reuse an idempotency key on retries."""
        return store.pay(debt_id, amount, date, interest, note, idempotency_key)

    @server.tool()
    def upcoming_payments(days: int = 7, as_of: str | None = None) -> dict[str, Any]:
        """Read estimated upcoming minimums within 0–366 days, accounting for actual payments already recorded in the month."""
        return store.due(days, as_of)

    return server


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
