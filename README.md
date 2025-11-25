# YNAB Checker

CLI that flags YNAB accounts that may go negative soon by looking at scheduled transactions.

## Requirements

- Python 3.10+
- `uv` (recommended runner)
- YNAB personal access token (`YNAB_TOKEN`)

## Usage

```bash
# inside this directory
# optionally load env vars from .env
set -a; [ -f .env ] && . .env; set +a

uv run ynab-checker --token "$YNAB_TOKEN"
```

Options:

- `--token` YNAB token. Defaults to `YNAB_TOKEN` env.
- `--budget-id` Budget to check. Defaults to `YNAB_BUDGET_ID` env or `last-used`.
- `--windows` Comma-separated day ranges to evaluate (default `3,7,30`).

The output lists each window and accounts projected to drop below zero based on current balance plus scheduled transactions up to that window. Accounts that stay non-negative are omitted for that window.

The CLI will also auto-load a local `.env` (simple `KEY=VALUE` lines) if present, without clobbering already-set environment variables.

Notes:

- Only cash/on-budget (non-debt) accounts are considered.
- An account is only reported if it has scheduled transactions within the specified window(s).
- Output is sorted by severity (most negative projected balance first) and shows the first date it is expected to drop below zero within each window.
- Suggested transfers section uses the longest requested window to propose moving available cash from surplus accounts to those projected to go negative, aiming to cover their drop dates. Available cash from a source is capped so that source never goes negative within that window. Surpluses are shared across accounts with the same earliest drop date so one account does not consume all available cash. Partial/insufficient coverage is not suggested; uncovered drops are listed separately.
