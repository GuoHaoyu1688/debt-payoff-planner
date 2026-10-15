# Version 0.1.0

Offline debt payoff simulations, exact-cent payment records, transparent APR calculations, CLI, MCP, and local dashboard..

Local verification: 44 tests passed; 93% measured coverage; Ruff lint and formatting checks passed. README quick start was exercised with fictional data.

## Known limitations

Monthly planning accrual and simplified explicit credit-card grace; opening balances and remaining terms are user-supplied snapshots. One two-decimal currency. No lender arrears reconstruction. Progress uses saved-plan milestones. Actual payment corrections require prior-backup restore and replay. Single-user loopback operation.

See README and AGENTS.md for installation, configuration, machine-readable CLI/MCP interfaces and operational boundaries.
