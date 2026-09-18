from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import httpx

from .agent.terminal import run_terminal
from .config import get_settings
from .db import transaction
from .observability import WorkflowTrace, configure_logging
from .providers import ProviderClients
from .services.delivery import process_pending_integrations
from .sftp_fixtures import load_stride_fixtures
from .worker import format_phone, materialize_cadence, run_tick

ROOT = Path(__file__).resolve().parents[2]
TEST_LEAD_RESET_SQL = ROOT / "supabase" / "dev" / "reset_test_lead_by_name.sql"


def migrate() -> None:
    trace = WorkflowTrace("database_migration", "cli")
    with transaction() as conn:
        conn.execute(
            "create table if not exists public.schema_migrations(version text primary key,applied_at timestamptz not null default now())"
        )
        applied = {row["version"] for row in conn.execute("select version from public.schema_migrations")}
        for path in sorted((ROOT / "supabase" / "migrations").glob("*.sql")):
            if path.name in applied:
                trace.log("migration_skipped", migration=path.name)
                continue
            trace.log("migration_started", migration=path.name)
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("insert into public.schema_migrations(version) values(%s)", (path.name,))
            trace.log("migration_completed", migration=path.name)
    trace.complete()


def verify() -> None:
    with transaction() as conn:
        rows = conn.execute("select version,applied_at from public.schema_migrations order by version").fetchall()
    print(json.dumps(rows, indent=2, default=str))


def seed() -> None:
    trace = WorkflowTrace("database_seed", "cli")
    with transaction() as conn:
        conn.execute((ROOT / "supabase" / "seed.sql").read_text(encoding="utf-8"))
    trace.complete()


def demo() -> None:
    settings = get_settings()
    if any(settings.mode(name) != "mock" for name in ("vapi", "twilio", "stride", "keap")):
        raise RuntimeError("rpt demo requires every provider mode to be mock; use `rpt test-lead` for a real Vapi call")
    trace = WorkflowTrace("local_demo", "cli")
    with transaction() as conn:
        practice = conn.execute("select id from practices where slug='rausch-pt'").fetchone()
        if not practice:
            raise RuntimeError("run `rpt seed` first")
        referral = f"demo-{uuid4().hex[:8]}"
        lead = conn.execute(
            "insert into leads(practice_id,source_system,external_referral_id,first_name,last_name,full_name,"
            "phone_e164,email,date_of_birth,timezone,status,cadence_state,is_test,test_run_id) "
            "values(%s,'demo',%s,'Synthetic','Patient','Synthetic Patient','+15555550123',"
            "'synthetic@example.test','1990-01-01','America/Los_Angeles','in_progress','active',true,%s) returning id",
            (practice["id"], referral, str(uuid4())),
        ).fetchone()
        materialize_cadence(conn, str(lead["id"]), practice["id"], datetime.now(UTC).date())
        call_event = conn.execute(
            "select id from outreach_events where lead_id=%s and channel='call' order by day_offset limit 1",
            (lead["id"],),
        ).fetchone()
    providers = ProviderClients()
    call_id = providers.create_vapi_call(trace, {
        "assistantId": "mock-assistant", "phoneNumberId": "mock-phone",
        "customer": {"number": "+15555550123"}, "assistantOverrides": {"variableValues": {
            "lead_id": str(lead["id"]), "outreach_event_id": str(call_event["id"]),
        }},
    })
    with transaction() as conn:
        conn.execute(
            "update outreach_events set status='attempted',executed_at=now(),provider='vapi',provider_ref=%s,"
            "vapi_call_id=%s where id=%s", (call_id, call_id, call_event["id"]),
        )
    base = get_settings().api_base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {get_settings().vapi_webhook_secret}", "X-Trace-ID": trace.trace_id}
    availability_request = {"message": {"type": "tool-calls", "toolCallList": [{
        "id": "demo-availability", "name": "get_available_slots",
        "arguments": {"lead_id": str(lead["id"])},
    }]}}
    response = httpx.post(base + "/api/v1/vapi/tools", json=availability_request, headers=headers, timeout=20)
    response.raise_for_status()
    availability = json.loads(response.json()["results"][0]["result"])
    if not availability["slots"]:
        raise RuntimeError("mock returned no slots")
    booking_request = {"message": {"type": "tool-calls", "toolCallList": [{
        "id": "demo-book", "name": "book_appointment", "arguments": {
            "lead_id": str(lead["id"]), "outreach_event_id": call_event["id"],
            "slot_token": availability["slots"][0]["slot_token"]
        },
    }]}}
    booked = httpx.post(base + "/api/v1/vapi/tools", json=booking_request, headers=headers, timeout=30)
    booked.raise_for_status()
    process_pending_integrations(trace, providers)
    trace.complete(lead_id=str(lead["id"]))
    print(json.dumps({"lead_id": str(lead["id"]), "availability": availability,
                      "booking": json.loads(booked.json()["results"][0]["result"])}, indent=2))


def create_test_lead(args: argparse.Namespace) -> None:
    settings = get_settings()
    if not settings.test_mode:
        raise RuntimeError("set TEST_MODE=true before creating an accelerated synthetic lead")
    phone = format_phone(args.phone)
    if not phone:
        raise ValueError("phone must be a valid E.164 or 10-digit North American number")
    if not args.consent_reference.strip():
        raise ValueError("--consent-reference is required before any test call")
    dob = date.fromisoformat(args.dob)
    test_run_id = uuid4()
    first_name = args.first_name.strip()
    last_name = args.last_name.strip()
    if not first_name or not last_name:
        raise ValueError("first and last name are required")
    with transaction() as conn:
        practice = conn.execute("select id from practices where slug='rausch-pt'").fetchone()
        if not practice:
            raise RuntimeError("run `rpt seed` first")
        active = conn.execute(
            "select oe.id from outreach_events oe join leads l on l.id=oe.lead_id "
            "where l.practice_id=%s and l.is_test is true and l.source_system='synthetic_test' "
            "and lower(btrim(coalesce(l.first_name,'')))=lower(btrim(%s)) "
            "and lower(btrim(coalesce(l.last_name,'')))=lower(btrim(%s)) "
            "and oe.status in ('in_flight','attempted') for update of oe",
            (practice["id"], first_name, last_name),
        ).fetchall()
        if active:
            raise RuntimeError(
                "cannot replace this synthetic lead while a call is in flight or awaiting settlement"
            )
        reset = conn.execute(
            TEST_LEAD_RESET_SQL.read_text(encoding="utf-8"),
            {
                "practice_slug": "rausch-pt",
                "first_name": first_name,
                "last_name": last_name,
            },
        ).fetchone()
        lead = conn.execute(
            "insert into leads(practice_id,source_system,external_referral_id,is_test,test_run_id,"
            "first_name,last_name,full_name,phone_e164,phone_original,date_of_birth,timezone,line_type,"
            "consent_captured_at,consent_source,consent_reference,consent_text_version,status,cadence_state) "
            "values(%s,'synthetic_test',%s,true,%s,%s,%s,%s,%s,%s,%s,'America/Los_Angeles','mobile',"
            "now(),'verbal_recorded',%s,'synthetic-test-v1','new','pending') returning id",
            (
                practice["id"], f"test-{test_run_id}", test_run_id, first_name,
                last_name, f"{first_name} {last_name}",
                phone, args.phone, dob, args.consent_reference.strip(),
            ),
        ).fetchone()
        event_count = materialize_cadence(
            conn, str(lead["id"]), practice["id"], datetime.now(UTC).date()
        )
        schedule = conn.execute(
            "select day_offset,channel,scheduled_for from outreach_events where lead_id=%s "
            "order by scheduled_for",
            (lead["id"],),
        ).fetchall()
    print(json.dumps({
        "lead_id": str(lead["id"]),
        "test_run_id": str(test_run_id),
        "replaced_test_leads": int(reset["deleted_leads"]),
        "event_count": event_count,
        "test_day_minutes": settings.test_cadence_day_minutes,
        "schedule": schedule,
    }, indent=2, default=str))


def main() -> None:
    configure_logging("cli")
    parser = argparse.ArgumentParser(prog="rpt")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("migrate", "verify", "seed", "demo", "fixtures", "tick"):
        subparsers.add_parser(command)
    test_lead = subparsers.add_parser("test-lead", help="create a consented synthetic lead")
    test_lead.add_argument("--phone", required=True)
    test_lead.add_argument("--first-name", default="Synthetic")
    test_lead.add_argument("--last-name", default="Patient")
    test_lead.add_argument("--dob", default="1990-01-01")
    test_lead.add_argument("--consent-reference", required=True)
    agent = subparsers.add_parser("agent", help="open the lead-scoped terminal assistant")
    agent.add_argument("--api-url")
    agent.add_argument("--lead", action="append", default=[])
    agent.add_argument("--user-id", default="terminal-agent")
    agent.add_argument("--email", default="terminal-agent@local.test")
    args = parser.parse_args()
    commands = {
        "migrate": migrate,
        "verify": verify,
        "seed": seed,
        "demo": demo,
        "fixtures": lambda: print(json.dumps(
            load_stride_fixtures(WorkflowTrace("sftp_fixture_import", "cli")), indent=2
        )),
        "tick": lambda: print(json.dumps(run_tick(), indent=2)),
        "test-lead": lambda: create_test_lead(args),
        "agent": lambda: run_terminal(
            api_url=args.api_url,
            initial_leads=args.lead,
            user_id=args.user_id,
            email=args.email,
        ),
    }
    commands[args.command]()


if __name__ == "__main__":
    main()
