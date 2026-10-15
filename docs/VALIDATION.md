# Validation record

Validated locally on 2026-10-15 with Python 3.12.13 on macOS. All records used in this verification are fictional.

## Automated checks

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest -q --cov=debt_payoff_planner --cov-report=term-missing
```

Result: 44 tests passed, 93% total statement coverage, and 19 Python files passed formatting. One upstream Starlette/httpx deprecation warning does not affect these results.

Independent arithmetic checks cover a 1,200.00 equal-installment loan at 12% nominal annual interest with two payments of 609.01 and 609.02 (18.03 total interest); equal-principal payments of 412.00, 408.00, and 404.00; an interest-only balloon; explicit credit-card grace; retained versus waived installment fees; and fee IRR verified against its discounted cash-flow equation. Tests also exercise zero interest, bounded non-amortizing scenarios, insufficient budgets, month-end dates, actual payment allocation, interest overrides, idempotency, concurrent writes, saved-plan progress, atomic import rollback, and database backup integrity.

The five-debt fixture produces these complete scenarios with a default monthly budget of 600.00 and its included monthly override:

| Strategy | Months | Payoff month | Total finance charges |
| --- | ---: | --- | ---: |
| Baseline | 118 | 2036-07 | 4,170.36 |
| Snowball | 18 | 2028-03 | 1,251.45 |
| Avalanche | 18 | 2028-03 | 1,128.82 |
| Custom | 18 | 2028-03 | 1,260.02 |

The baseline deliberately leaves unused budget unallocated. Savings are reported only when both compared scenarios finish.

## Interfaces and packaging

- Executed the README import, comparison, and Markdown report commands against a fresh example database.
- Checked JSON and CSV snapshot round trips, including payment journals and saved plans.
- Exercised all five MCP tools through the official SDK and performed a real stdio handshake/tool call in a child process.
- Verified CLI error envelopes and exit codes, environment/configuration precedence, REST validation, and local Host/Origin protections.
- Built a wheel and inspected its 20 entries, including the packaged HTML, CSS, and JavaScript. No database was included.
- Visually reviewed both generated balance and stacked-debt charts.

## Browser verification

Ran headless installed Google Chrome through Playwright at 1440 × 1000 and 390 × 844. Verified debt search, strategy changes, monthly allocation details, report preview, saving a plan, recording a payment, plan progress, and insufficient-budget feedback. There were no browser console errors, page exceptions, external network requests, or document-level horizontal overflow. Wide tables scroll within their panels. Screenshots are [desktop](dashboard.png) and [mobile](mobile.png).

A browser scenario exposed a useful planning distinction: paying 100.00 early toward the personal loan leaves only 500.00 of a 600.00 monthly cash budget, while the remaining mandatory payments total 547.15. The resulting 47.15 shortfall is correct and now has a dedicated regression test. A 700.00 budget resolves that scenario.

## Scope and assumptions

This is a local planning ledger, not a bank statement reconciler. Monthly nominal-interest accrual, explicit simplified grace dates, and fee retention/waiver rules are described in the README. Opening balances belong to the stated snapshot date, and remaining terms belong to that snapshot. The tool does not reconstruct arrears, infer lender-specific daily compounding, or calculate jurisdiction-specific statutory APR. Plan lead/lag is a principal-milestone comparison. Actual payments are immutable; correcting a history mistake currently requires restoring a prior backup and replaying subsequent entries. Restore is a documented manual operation. The loopback web application is single-user and has no account system; one ledger uses one two-decimal currency.
