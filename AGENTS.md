# Headless debt-planning operations

Use this offline SQLite application to compare repayment strategies, calculate fee IRR, and record actual principal/finance-charge payments. It never connects to a lender or transfers money. All bundled data is fictional.

## Install on Linux

Use Python 3.11 or newer:

```bash
git clone https://github.com/GuoHaoyu1688/debt-payoff-planner.git
cd debt-payoff-planner
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
export DEBT_DATA_DIR=/workspace/data/debt-payoff-planner
debt init --json
```

No system OCR, browser, or PDF tools are needed. The example deployment paths above are not hardcoded. Configuration uses `--config PATH` or `DEBT_CONFIG`; data uses `--data-dir DIR` or `DEBT_DATA_DIR`. CLI data directory wins over environment, then YAML, then `./data`. Relative YAML data paths resolve beside the config. The database is `<data-dir>/debts.db`; dated ZIP backups and Markdown/PNG reports are in `backups/` and `reports/`. Defaults are Asia/Shanghai and CNY. `default_budget` is optional; if absent, simulations require an explicit budget.

## Invocation and JSON contract

All commands are noninteractive. Common flags are `--json`, `--quiet`, `--progress`, `--version`, `--data-dir`, and `--config`, and may appear before or after the subcommand. Progress logs go to stderr. JSON output is one object on stdout. Monetary output uses integer cents; monetary and rate input uses decimal strings. Rates accept `0.12` or `12%`.

Exact output examples on a fresh data directory:

```bash
debt list --json
# {"ok": true, "data": [], "warnings": []}
debt budget list --json
# {"ok": true, "data": {}, "warnings": []}
debt pay MISSING --amount 1 --date 2026-10-01 --json
# {"ok": false, "error": {"code": "NOT_FOUND", "message": "debt not found"}}
```

The last command exits 3. Exit codes: 0 command success, 1 I/O/database error, 2 validation/conflict, 3 missing debt/plan. **Simulation success is not affordability:** always inspect `complete` and `failure`. `MINIMUM_BUDGET_SHORTFALL` includes required, available, and missing cents; `HORIZON_EXCEEDED` reports an incomplete bounded run. Savings are null when the baseline or comparison is incomplete.

## Command map

| Task | Command |
| --- | --- |
| Initialize / inspect | `debt init --json`; `debt list --json` |
| Add fixed installment | `debt add --code LOAN-X --type equal_installment --balance 1200 --annual-rate 12% --term-months 2 --start-date 2026-10-01 --due-day 15 --json` |
| Add equal principal | `debt add --code LOAN-Y --type equal_principal --balance 1200 --annual-rate 12% --term-months 3 --start-date 2026-10-01 --json` |
| Add interest-only | `debt add --code BALLOON-Z --type interest_only --balance 1200 --annual-rate 12% --term-months 3 --start-date 2026-10-01 --json` |
| Add credit card | `debt add --code CARD-X --type credit_card --balance 1000 --daily-rate 0.05% --minimum 50 --minimum-ratio 10% --start-date 2026-10-01 --grace-until 2026-10-31 --json` |
| Add fee installment | `debt add --code FEE-X --type fee_installment --balance 1200 --fee-rate 0.6% --term-months 12 --fee-policy retain --start-date 2026-10-01 --json` |
| Add flexible debt | `debt add --code PERSONAL-X --type personal --balance 600 --minimum 20 --priority 1 --start-date 2026-10-01 --json` |
| Update assumptions | `debt update PERSONAL-X --data '{"minimum":"30","priority":0,"note":"Fictional revised agreement"}' --json` |
| Record actual payment | `debt pay PERSONAL-X --amount 100 --date 2026-10-10 --interest 0 --idempotency-key fictional-payment-1 --json` |
| Inspect actual journal | `debt payments --json` |
| Set/list budget overrides | `debt budget set 2026-12 --amount 1000 --json`; `debt budget list --json` |
| Simulate and save | `debt simulate --strategy avalanche --budget 600 --start 2026-10 --save --json` |
| Custom order | `debt simulate --strategy custom --budget 600 --start 2026-10 --order PERSONAL-E,FEE-D,LOAN-C,LOAN-B,CARD-A --json` |
| Compare four paths | `debt compare --budget 600 --start 2026-10 --months 600 --json` |
| Evaluate an extra payment | `debt extra --amount 1000 --month 2026-12 --budget 600 --start 2026-10 --json` |
| Fee IRR | `debt apr --fee-rate 0.6% --months 12 --json`; optionally `--fee-mode total` or `upfront` |
| Daily-rate conversion | `debt apr --daily-rate 0.05% --json` |
| Upcoming minimums | `debt due --within 7d --as-of 2026-10-10 --json` |
| Saved plans / progress | `debt plans --json`; `debt progress --plan PLAN_ID --as-of 2026-10-15 --json` |
| Report and charts | `debt report --budget 600 --start 2026-10 --md --json`; optional `--out PATH` |
| Snapshot import | `debt import examples/five_debts.json --json`; `.csv` also accepted |
| Snapshot export | `debt export --format csv --out snapshot.csv --json`; `--utf8` omits the default BOM |
| Full backup | `debt backup --json`; optional `--out PATH` |
| Local API/UI | `debt serve --host 127.0.0.1 --port 8768` |
| MCP | `debt mcp` |

Read `debt COMMAND --help` for argument details. Update supports annual_rate, minimum, due_day, maturity_date, priority, note, minimum_ratio, grace_until, and fee_policy. It cannot directly alter principal, term, or payment history. Changing an equal-installment rate recomputes the payment for the remaining calendar term. Custom explicit order must include every debt code exactly once; without it, numeric priority is used.

## Accounting assumptions that agents must preserve

- `start_date` is the opening balance snapshot date, and `term_months` is the remaining term. Do not use a historical origination date with today's balance unless the intervening payment history is also supplied.
- Finance charges are monthly, nominal annual / 12, using Decimal and half-up cent rounding. Daily-rate input converts with a 365-day year. It is not actual daily statement accrual.
- Minimums and maturity balloons are paid before acceleration. Monthly overrides replace the default total, and actual payments already recorded that month consume it. Unused budget does not roll forward.
- Grace is an explicit simplified date with no retroactive grace-loss interest. Flat fees apply to original principal; default retention keeps future fees owed even after principal payoff. Do not infer fee waiver.
- Actual payments reduce local balances only. Optional `--interest` replaces **newly assessed** charges; previously assessed unpaid charges remain due. Missing months' estimated interest is not silently ignored. This is not a full lender arrears model; overdue principal installments and penalties need statement-based inputs.
- Actual payments must be chronological and cannot be future-dated or exceed the obligation. The journal retains exact principal/charge allocation and before/after snapshots. Progress days are scheduled milestones, not legal delinquency days.
- Incomplete paths have no payoff date. Never quote partial lifetime interest as a full cost or null savings as zero.

## Idempotency, snapshots, and recovery

`pay --idempotency-key KEY` returns the original payment for identical retries and rejects conflicting content. Plain `add`, `pay` without a key, and `simulate --save` create new records. Budget set is an upsert. Read-only simulate/compare/extra/APR commands do not change balances. Saved plans are immutable.

Imports are atomic snapshots of `debts`, `payments`, `budgets`, and `plans`, selected by `record_type`. Debt/payment/plan rows require stable IDs; budget uses `month`. Use complete exports as format references. Cents are integers; interest rates are strings; embedded journal/plan snapshots are JSON strings. Import never subtracts payment principal again. Identical rows are skipped, conflicts fail, and invalid payment allocations or final balances roll back the batch. Reimporting an old example after recording payments correctly conflicts instead of resetting user balances.

Back up before changing assumptions or budget overrides, restoring snapshots, correcting a mistaken payment, or overwriting exported/report files. There are no hard-delete or payment-reversal commands in v0.1.0. To correct a mistake, restore the prior full backup to a fresh directory and replay verified entries. A ZIP contains a consistent `debts.db` and settings JSON. Stop processes before restoring and set the new data directory/config; do not overwrite a live SQLite database. SQLite uses WAL and immediate write transactions, so concurrent retry keys cannot double-post a payment.

The Web API is loopback-only, without authentication. Do not expose it through an untrusted proxy. Use a trusted local account. Reports and CSV are unencrypted; user data stays in the configured directory.

## MCP

```json
{
  "mcpServers": {
    "debt-planner": {
      "command": "/workspace/apps/debt-payoff-planner/.venv/bin/debt",
      "args": ["--data-dir", "/workspace/data/debt-payoff-planner", "mcp"]
    }
  }
}
```

`python -m debt_payoff_planner.mcp_server` also works using environment settings. The official SDK generates JSON Schemas from these signatures:

| Tool | Parameters and effects |
| --- | --- |
| `list_debts` | No parameters; read current snapshots. |
| `simulate_plan` | Required strings `strategy`, `budget`; optional nullable string `start`, integer `months=600` (1–1200), nullable list of strings `order`; read-only. |
| `compare_strategies` | Required string `budget`; nullable `start`, integer `months=600`; read-only. |
| `record_payment` | Required strings `debt_id`, `amount`, `date`; nullable string `interest`, string `note=""`, nullable string `idempotency_key`; writes the local payment journal and current balances. |
| `upcoming_payments` | Integer `days=7` (0–366), nullable ISO-date string `as_of`; read-only. |

Tools return JSON structured content. List results may be wrapped under `result` by the SDK. Invalid inputs are MCP tool errors. The stdio channel carries protocol only; no status text goes to stdout. Every tool and a real subprocess handshake are tested.

## Recipes

1. **“Which strategy clears my debts?”** Verify the opening balance date, term, minimums, fee policy, and monthly budget. Run `compare --budget ... --json`; inspect each `complete`/`failure` before comparing finance charges and dates.
2. **“My commission adds money in December.”** Use `budget set` when changing the total December budget, or `extra` for an additional one-off amount above the configured budget. Report the tested assumptions and whether all candidate paths are complete.
3. **“I paid this statement.”** Obtain actual payment date, total, and newly assessed statement charges. Record with a stable retry key. Inspect returned principal and charge allocation; never imply an external payment was executed.
4. **“Am I ahead of the plan?”** Read `plans`, select the previously saved snapshot, and call `progress --plan ... --as-of ... --json`. Report both principal differences and the milestone-based day estimate; retained fees may remain after principal is zero.
5. **“This installment has only a small fee.”** Use `apr --fee-rate ... --months ...`; clarify monthly/total/upfront convention. Report nominal annualized IRR separately from effective annual yield and quoted flat fee. Do not label this a statutory lender APR.

## Test and review

```bash
python -m pip install -e '.[dev]'
ruff check .
ruff format --check .
pytest -q --cov=debt_payoff_planner --cov-report=term-missing
```

Use temporary data directories for tests. No network is required after dependency installation. The reproducible five-debt comparison snapshot lives in `examples/expected_comparison.json`; independent hand-calculated tests protect the formulas separately from that regression fixture. See `docs/VALIDATION.md` for observed local checks. No live CI result or publication should be claimed until verified.
