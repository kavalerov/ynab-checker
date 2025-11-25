#!/usr/bin/env python3
"""YNAB account risk checker."""

from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
from datetime import date, timedelta
from typing import Dict, Iterable, List, Sequence, Tuple
from urllib.error import HTTPError
from urllib.request import Request, urlopen


API_BASE = "https://api.ynab.com/v1"
DEFAULT_WINDOWS = (3, 7, 30)
DOTENV_PATH = ".env"
GBP_SYMBOL = "£"


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def load_dotenv(path: str = DOTENV_PATH) -> None:
    """Minimal .env loader (KEY=VALUE lines)."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key and key not in os.environ:
                os.environ[key] = value


def is_cash_account(account: dict) -> bool:
    """Return True for on-budget, non-debt accounts."""
    non_cash_types = {
        "creditCard",
        "lineOfCredit",
        "mortgage",
        "autoLoan",
        "studentLoan",
        "personalLoan",
        "medicalDebt",
        "otherDebt",
        "otherLiability",
    }
    acct_type = account.get("type")
    if acct_type in non_cash_types:
        return False
    return account.get("on_budget", True)


def build_occurrences(
    scheduled: Sequence[dict], max_window: int
) -> Dict[str, List[Tuple[date, int]]]:
    today = date.today()
    occurrences: Dict[str, List[Tuple[date, int]]] = {}
    for txn in scheduled:
        account_id = txn.get("account_id")
        if not account_id:
            continue
        amount = int(txn.get("amount", 0))
        if txn.get("scheduled_subtransactions"):
            amount = sum(int(st.get("amount", 0)) for st in txn["scheduled_subtransactions"])
        if amount == 0:
            continue
        txn_date = date.fromisoformat(txn["date_next"])
        frequency = txn.get("frequency", "never")
        for occurrence in occurrences_within_window(txn_date, frequency, max_window):
            if occurrence < today:
                continue
            occurrences.setdefault(account_id, []).append((occurrence, amount))
    for acct_occurrences in occurrences.values():
        acct_occurrences.sort(key=lambda item: item[0])
    return occurrences


def calc_projection(
    acct_occurrences: Sequence[Tuple[date, int]], balance: int, cutoff: date
) -> Tuple[int, date | None]:
    projected = balance
    drop_date: date | None = None
    for when, amount in acct_occurrences:
        if when > cutoff:
            break
        projected += amount
        if projected < 0 and drop_date is None:
            drop_date = when
    return projected, drop_date


def request_ynab(path: str, token: str) -> dict:
    url = f"{API_BASE}{path}"
    req = Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urlopen(req) as resp:  # noqa: S310 - external API request is expected
            payload = resp.read().decode("utf-8")
    except HTTPError as exc:  # pragma: no cover - network error handling
        body = exc.read().decode("utf-8")
        raise SystemExit(f"YNAB API request failed ({exc.code}): {body}") from exc
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise SystemExit(f"Unexpected response from YNAB: {exc}") from exc
    return data.get("data", {})


def fetch_accounts(budget_id: str, token: str) -> List[dict]:
    data = request_ynab(f"/budgets/{budget_id}/accounts", token)
    accounts = data.get("accounts", [])
    return [acct for acct in accounts if not acct.get("deleted") and not acct.get("closed")]


def fetch_scheduled_transactions(budget_id: str, token: str) -> List[dict]:
    data = request_ynab(f"/budgets/{budget_id}/scheduled_transactions", token)
    return data.get("scheduled_transactions", [])


def add_months(src: date, months: int) -> date:
    month_index = src.month - 1 + months
    year = src.year + month_index // 12
    month = month_index % 12 + 1
    day = min(src.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def advance_date(current: date, frequency: str) -> date | None:
    if frequency == "never":
        return None
    if frequency == "daily":
        return current + timedelta(days=1)
    if frequency == "weekly":
        return current + timedelta(days=7)
    if frequency == "everyOtherWeek":
        return current + timedelta(days=14)
    if frequency == "every4Weeks":
        return current + timedelta(days=28)
    if frequency == "twiceAMonth":
        return current + timedelta(days=15)
    if frequency == "monthly":
        return add_months(current, 1)
    if frequency == "everyOtherMonth":
        return add_months(current, 2)
    if frequency == "every3Months":
        return add_months(current, 3)
    if frequency == "every4Months":
        return add_months(current, 4)
    if frequency == "twiceAYear":
        return add_months(current, 6)
    if frequency == "yearly":
        return add_months(current, 12)
    if frequency == "everyOtherYear":
        return add_months(current, 24)
    return None


def occurrences_within_window(start: date, frequency: str, max_days: int) -> Iterable[date]:
    today = date.today()
    cutoff = today + timedelta(days=max_days)
    current = start
    while current <= cutoff:
        if current >= today:
            yield current
        next_date = advance_date(current, frequency)
        if not next_date or next_date <= current:
            break
        current = next_date


def milliunits_to_str(amount: int) -> str:
    sign = "-" if amount < 0 else ""
    abs_amount = abs(amount) / 1000
    return f"{sign}{GBP_SYMBOL}{abs_amount:,.2f}"


def compute_risk(
    accounts: Sequence[dict], occurrences: Dict[str, List[Tuple[date, int]]], windows: Sequence[int]
) -> Dict[int, List[dict]]:
    today = date.today()
    sorted_windows = sorted(set(windows))
    risks: Dict[int, List[dict]] = {w: [] for w in sorted_windows}
    for account in accounts:
        account_id = account["id"]
        if not is_cash_account(account):
            continue
        acct_occurrences = occurrences.get(account_id)
        if not acct_occurrences:
            continue
        balance = int(account.get("balance", 0))
        for win in sorted_windows:
            cutoff = today + timedelta(days=win)
            projected, drop_date = calc_projection(acct_occurrences, balance, cutoff)
            if drop_date:
                risks[win].append(
                    {
                        "name": account.get("name", account_id),
                        "projected": projected,
                        "current": balance,
                        "window": win,
                        "drop_date": drop_date,
                    }
                )
    # Sort each window's risks by severity (most negative projected balance first)
    for win in risks:
        risks[win].sort(key=lambda entry: entry["projected"])
    return risks


def print_report(risks: Dict[int, List[dict]]) -> None:
    any_risk = False
    for window in sorted(risks):
        entries = risks[window]
        header = f"Accounts at risk within {window} days"
        print(header)
        print("-" * len(header))
        if not entries:
            print("None\n")
            continue
        any_risk = True
        for entry in entries:
            name = entry["name"]
            current = milliunits_to_str(entry["current"])
            projected = milliunits_to_str(entry["projected"])
            drop_date = entry.get("drop_date")
            drop_str = drop_date.isoformat() if drop_date else "n/a"
            print(f"{name}: drop {drop_str}, projected {projected} (current {current})")
        print()
    if not any_risk:
        print("All accounts stay non-negative in the selected windows.")


def compute_transfers(
    accounts: Sequence[dict], occurrences: Dict[str, List[Tuple[date, int]]], windows: Sequence[int]
) -> Tuple[List[dict], List[dict]]:
    """Suggest transfers using the longest window projection.

    Only emit moves when there is enough surplus to fully cover the grouped deficit
    (by earliest drop date). Partial coverage suggestions are skipped.
    """
    if not windows:
        return [], []
    today = date.today()
    cutoff = today + timedelta(days=max(windows))
    surpluses: List[dict] = []
    deficits: List[dict] = []
    for account in accounts:
        if not is_cash_account(account):
            continue
        acct_occurrences = occurrences.get(account["id"], [])
        balance = int(account.get("balance", 0))
        projected, drop_date = calc_projection(acct_occurrences, balance, cutoff)
        if projected > 0:
            surpluses.append({"id": account["id"], "name": account.get("name", ""), "available": projected})
        elif projected < 0:
            drop_date = drop_date or today
            deficits.append(
                {
                    "id": account["id"],
                    "name": account.get("name", ""),
                    "need": -projected,
                    "drop_date": drop_date,
                }
            )

    surpluses.sort(key=lambda s: s["available"], reverse=True)
    deficits.sort(key=lambda d: (d["drop_date"] or cutoff, -d["need"]))

    moves: List[dict] = []
    uncovered: List[dict] = []
    # Group deficits by drop date to share limited surplus across accounts dropping at the same time.
    idx = 0
    while idx < len(deficits):
        drop_date = deficits[idx]["drop_date"]
        group: List[dict] = []
        while idx < len(deficits) and deficits[idx]["drop_date"] == drop_date:
            group.append(deficits[idx])
            idx += 1

        available_pool = sum(s["available"] for s in surpluses if s["available"] > 0)
        total_need = sum(d["need"] for d in group if d["need"] > 0)
        if available_pool <= 0 or total_need <= 0:
            if total_need > 0:
                uncovered.append(
                    {"drop_date": drop_date, "need": total_need, "available": available_pool, "accounts": group}
                )
            continue

        # Skip suggesting partial coverage; require full coverage for this drop date group.
        if available_pool < total_need:
            uncovered.append(
                {"drop_date": drop_date, "need": total_need, "available": available_pool, "accounts": group}
            )
            continue

        allocatable = min(available_pool, total_need)
        allocations = [0 for _ in group]

        # Initial proportional allocation based on need.
        for i, deficit in enumerate(group):
            share = allocatable * deficit["need"] // total_need
            allocations[i] = min(deficit["need"], share)

        # Distribute any remainder to largest unmet needs.
        remainder = allocatable - sum(allocations)
        for i, deficit in sorted(enumerate(group), key=lambda item: item[1]["need"], reverse=True):
            if remainder <= 0:
                break
            room = deficit["need"] - allocations[i]
            if room <= 0:
                continue
            extra = min(room, remainder)
            allocations[i] += extra
            remainder -= extra

        # Execute allocations from the surplus pool.
        for alloc, deficit in zip(allocations, group):
            if alloc <= 0:
                continue
            remaining = alloc
            for source in surpluses:
                if remaining <= 0:
                    break
                if source["available"] <= 0:
                    continue
                amount = min(remaining, source["available"])
                if amount <= 0:
                    continue
                source["available"] -= amount
                remaining -= amount
                moves.append(
                    {
                        "from": source["name"],
                        "to": deficit["name"],
                        "amount": amount,
                        "cover_drop": deficit.get("drop_date"),
                    }
                )
    return moves, uncovered


def print_transfers(moves: Sequence[dict], uncovered: Sequence[dict]) -> None:
    header = "Suggested cover transfers (longest window projection; only full coverage shown)"
    print(header)
    print("-" * len(header))
    if moves:
        for move in moves:
            amount = milliunits_to_str(move["amount"])
            cover = move.get("cover_drop")
            cover_str = f", covers drop on {cover.isoformat()}" if cover else ""
            print(f"Move {amount} from {move['from']} to {move['to']}{cover_str}")
        print()
    else:
        print("None\n")
    if uncovered:
        print("Uncovered drops (insufficient surplus, no partial moves suggested):")
        for item in uncovered:
            need = milliunits_to_str(item["need"])
            avail = milliunits_to_str(item["available"])
            when = item["drop_date"].isoformat() if item["drop_date"] else "unknown"
            names = ", ".join(acct["name"] for acct in item.get("accounts", []))
            print(f"{when}: need {need}, available {avail} :: {names}")
        print()


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Flag YNAB accounts that may go negative soon.")
    parser.add_argument(
        "--token",
        default=_env("YNAB_TOKEN"),
        help="YNAB personal access token (default: YNAB_TOKEN env var)",
    )
    parser.add_argument(
        "--budget-id",
        default=_env("YNAB_BUDGET_ID", "last-used"),
        help="Budget ID to check (default: YNAB_BUDGET_ID env var or 'last-used')",
    )
    parser.add_argument(
        "--windows",
        default=",".join(str(w) for w in DEFAULT_WINDOWS),
        help="Comma-separated day windows to evaluate (default: 3,7,30)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    load_dotenv()
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if not args.token:
        raise SystemExit("YNAB token is required (pass --token or set YNAB_TOKEN).")
    try:
        windows = [int(part) for part in str(args.windows).split(",") if part.strip()]
    except ValueError as exc:
        raise SystemExit(f"Invalid --windows value: {args.windows}") from exc
    accounts = fetch_accounts(args.budget_id, args.token)
    scheduled = fetch_scheduled_transactions(args.budget_id, args.token)
    max_window = max(windows)
    occurrences = build_occurrences(scheduled, max_window)
    risks = compute_risk(accounts, occurrences, windows)
    transfers, uncovered = compute_transfers(accounts, occurrences, windows)
    print_report(risks)
    print_transfers(transfers, uncovered)


if __name__ == "__main__":  # pragma: no cover
    main()
