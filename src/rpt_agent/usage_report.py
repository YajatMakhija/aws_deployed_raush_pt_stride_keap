from __future__ import annotations

import hashlib
import hmac
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from .config import get_settings
from .db import transaction

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT = ROOT / "docs" / "CLIENT_TEST_USAGE.md"


def recipient_label(phone: str, secret: str) -> str:
    digits = "".join(character for character in phone if character.isdigit())
    fingerprint = hmac.new(secret.encode(), phone.encode(), hashlib.sha256).hexdigest()[:10]
    return f"Tester ***{digits[-4:]} ({fingerprint})"


def _cost(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return abs(Decimal(str(value)))
    except InvalidOperation:
        return None


def record_test_usage(conn, provider: str, usage_type: str, lead_id: str, provider_ref: str) -> None:
    conn.execute(
        "insert into test_usage_ledger(lead_id,test_run_id,provider,usage_type,recipient_e164,"
        "provider_ref,status) select id,test_run_id,%s,%s,phone_e164,%s,'accepted' from leads "
        "where id=%s and is_test is true on conflict(provider,provider_ref) do nothing",
        (provider, usage_type, provider_ref, lead_id),
    )
    if provider == "vapi":
        conn.execute(
            "update test_usage_ledger u set status='ended',outcome=oe.outcome,"
            "finalized_at=coalesce(u.finalized_at,now()) from outreach_events oe "
            "where u.provider='vapi' and u.provider_ref=%s and oe.vapi_call_id=u.provider_ref "
            "and exists(select 1 from call_logs cl where cl.vapi_call_id=u.provider_ref)",
            (provider_ref,),
        )


def refresh_provider_usage() -> None:
    settings = get_settings()
    with transaction() as conn:
        rows = conn.execute(
            "select id,provider,provider_ref from test_usage_ledger order by id"
        ).fetchall()
    with httpx.Client(timeout=30) as client:
        for row in rows:
            if row["provider"] == "vapi":
                response = client.get(
                    f"{settings.vapi_base_url.rstrip('/')}/call/{row['provider_ref']}",
                    headers={"Authorization": f"Bearer {settings.vapi_api_key}"},
                )
                response.raise_for_status()
                data = response.json()
                status = str(data.get("status") or "accepted")
                outcome = str(data.get("endedReason") or "") or None
                price = _cost(data.get("cost"))
                currency = "USD" if price is not None else None
            else:
                response = client.get(
                    f"https://api.twilio.com/2010-04-01/Accounts/"
                    f"{settings.twilio_account_sid}/Messages/{row['provider_ref']}.json",
                    auth=(settings.twilio_account_sid, settings.twilio_auth_token),
                )
                response.raise_for_status()
                data = response.json()
                status = str(data.get("status") or "accepted")
                outcome = str(data.get("error_code") or "") or None
                price = _cost(data.get("price"))
                currency = str(data.get("price_unit") or "").upper() or None
            terminal = status in {"ended", "delivered", "failed", "undelivered", "canceled"}
            with transaction() as conn:
                conn.execute(
                    "update test_usage_ledger set status=%s,outcome=coalesce(%s,outcome),"
                    "provider_cost=coalesce(%s,provider_cost),currency=coalesce(%s,currency),"
                    "finalized_at=case when %s then coalesce(finalized_at,now()) else finalized_at end "
                    "where id=%s",
                    (status, outcome, price, currency, terminal, row["id"]),
                )


def render_report() -> str:
    settings = get_settings()
    with transaction() as conn:
        rows = conn.execute(
            "select provider,usage_type,recipient_e164,provider_ref,status,outcome,provider_cost,"
            "currency,accepted_at from test_usage_ledger order by accepted_at,provider,id"
        ).fetchall()
    totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    counts: dict[str, int] = defaultdict(int)
    pending_counts: dict[str, int] = defaultdict(int)
    recipient_counts: dict[tuple[str, str], int] = defaultdict(int)
    recipient_pending: dict[str, int] = defaultdict(int)
    recipient_totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for row in rows:
        counts[row["provider"]] += 1
        recipient_counts[(row["recipient_e164"], row["provider"])] += 1
        if row["provider_cost"] is not None:
            currency = row["currency"] or "USD"
            cost = Decimal(row["provider_cost"])
            totals[(row["provider"], currency)] += cost
            recipient_totals[(row["recipient_e164"], currency)] += cost
        else:
            pending_counts[row["provider"]] += 1
            recipient_pending[row["recipient_e164"]] += 1
    lines = [
        "# Client Testing Usage Report",
        "",
        f"Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "",
        "This report tracks real, potentially chargeable provider activity generated only for synthetic",
        "project testing. Recipient numbers are masked; the stable fingerprint distinguishes testers without",
        "putting a full phone number in source control or client documentation.",
        "",
        "## Summary",
        "",
        "| Provider | Billable operations | Provider-reported cost |",
        "|---|---:|---:|",
    ]
    for provider in ("vapi", "twilio"):
        provider_totals = [
            f"{currency} {amount:.4f}"
            for (name, currency), amount in sorted(totals.items())
            if name == provider
        ]
        lines.append(
            f"| {provider.title()} | {counts[provider]} | "
            f"{', '.join(provider_totals) if provider_totals else 'USD 0.0000'}"
            f"{f' + {pending_counts[provider]} pending' if pending_counts[provider] else ''} |"
        )
    grand_usd = sum(
        amount for (provider, currency), amount in totals.items() if currency == "USD"
    )
    pending_total = sum(pending_counts.values())
    lines.extend([
        (
            f"| **Total** | **{len(rows)}** | **USD {grand_usd:.4f}"
            f"{f' + {pending_total} pending' if pending_total else ''}** |"
        ),
        "",
        "## Recipient breakdown",
        "",
        "| Recipient | Calls | SMS | Provider-reported cost |",
        "|---|---:|---:|---:|",
    ])
    for phone in sorted({row["recipient_e164"] for row in rows}, key=lambda value: value[-4:]):
        phone_totals = [
            f"{currency} {amount:.4f}"
            for (recipient, currency), amount in sorted(recipient_totals.items())
            if recipient == phone
        ]
        lines.append(
            f"| {recipient_label(phone, settings.slot_token_secret)} | "
            f"{recipient_counts[(phone, 'vapi')]} | {recipient_counts[(phone, 'twilio')]} | "
            f"{', '.join(phone_totals) if phone_totals else 'USD 0.0000'}"
            f"{f' + {recipient_pending[phone]} pending' if recipient_pending[phone] else ''} |"
        )
    lines.extend([
        "",
        "## Detailed usage",
        "",
        "| UTC timestamp | Provider | Type | Recipient | Provider reference | Status / outcome | Cost |",
        "|---|---|---|---|---|---|---:|",
    ])
    for row in rows:
        timestamp = row["accepted_at"].astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
        label = recipient_label(row["recipient_e164"], settings.slot_token_secret)
        state = row["status"] + (f" / {row['outcome']}" if row["outcome"] else "")
        price = (
            f"{row['currency'] or 'USD'} {Decimal(row['provider_cost']):.4f}"
            if row["provider_cost"] is not None
            else "Pending"
        )
        lines.append(
            f"| {timestamp} | {row['provider'].title()} | {row['usage_type']} | {label} | "
            f"`{row['provider_ref']}` | {state} | {price} |"
        )
    lines.extend([
        "",
        "## Interpretation",
        "",
        "- A zero-cost Vapi row is retained because the provider accepted and executed the test operation.",
        "- Twilio returns message prices as negative ledger debits; this report displays absolute spend.",
        "- Provider prices can settle after delivery. Regenerate the report before sending it to the client.",
        "- Taxes, account credits, carrier adjustments, and later provider corrections may differ from these",
        "  API-reported values. The provider invoice remains the accounting source of truth.",
        "- Mock Stride and mock Keap operations have no external provider cost and are not counted here.",
        "",
        "## Regenerate",
        "",
        "```powershell",
        "$env:PYTHONPATH = \"src\"",
        "python scripts/generate_test_usage_report.py",
        "```",
        "",
    ])
    return "\n".join(lines)


def generate_report(path: Path = DEFAULT_REPORT) -> Path:
    refresh_provider_usage()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(), encoding="utf-8")
    return path


def main() -> None:
    print(generate_report())


if __name__ == "__main__":
    main()
