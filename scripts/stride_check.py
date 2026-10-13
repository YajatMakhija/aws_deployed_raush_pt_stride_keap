"""Show everything our system knows about one test lead, to check a Stride test case from the terminal.

    uv run --frozen python scripts/stride_check.py "Lead One"          # by name (part of it is fine)
    uv run --frozen python scripts/stride_check.py 4e9b6bf1            # by lead id (first 8 characters)
    uv run --frozen python scripts/stride_check.py --files             # the last Stride files our server loaded

Read-only. Uses the database in .env (the demo database). Prints, for each matching lead:
status and review reason, calls/texts left, the linked Stride patient, every Stride visit we hold for
that patient (active / cancelled / deleted), the last status changes, and the last Sheet updates.
Phone numbers are shortened.
"""

from __future__ import annotations

import argparse
import re

import psycopg
from psycopg.rows import dict_row

from rpt_agent.config import get_settings


def short_phone(value: str | None) -> str:
    return f"{value[:3]}…{value[-2:]}" if value else "-"


def show_lead(conn, lead: dict) -> None:
    print("=" * 78)
    print(f"Lead {str(lead['id'])[:8]}  {lead['full_name']}  phone {short_phone(lead['phone_e164'])}  "
          f"DOB {lead['date_of_birth'] or '-'}  source {lead['source_system']}")
    print(f"  Status: {lead['status']}  | cadence: {lead['cadence_state']}  | needs review: {lead['needs_review']}"
          + (f"  | reason: {lead['review_reason']}" if lead["review_reason"] else ""))
    left = conn.execute("select count(*) n from outreach_events where lead_id=%s and status='planned'",
                        (lead["id"],)).fetchone()["n"]
    print(f"  Calls/texts still planned: {left}")
    patient = conn.execute(
        "select stride_id,first_name,last_name,date_of_birth,lead_match_status,deleted_at from stride_patients "
        "where lead_id=%s or stride_id=%s", (lead["id"], lead["stride_patient_id"])).fetchone()
    if not patient:
        candidates = conn.execute(
            "select stride_id,lead_match_status from stride_patients where lower(first_name||' '||last_name) "
            "= lower(%s) and deleted_at is null", (lead["full_name"],)).fetchall()
        hint = ", ".join(f"{c['stride_id']} ({c['lead_match_status']})" for c in candidates) or "none with this name"
        print(f"  Stride patient: NOT LINKED (Stride patients with the same name: {hint})")
    else:
        print(f"  Stride patient: {patient['stride_id']}  {patient['first_name']} {patient['last_name']}  "
              f"DOB {patient['date_of_birth']}  match: {patient['lead_match_status']}"
              + ("  (deleted in Stride)" if patient["deleted_at"] else ""))
        visits = conn.execute(
            "select stride_id,case_stride_id,starts_at_local,status,deleted_at,start_utc>now() as future,"
            "to_char(updated_at,'MM-DD HH24:MI') changed from stride_appointments "
            "where patient_stride_id=%s order by starts_at_local", (patient["stride_id"],)).fetchall()
        print(f"  Stride visits we hold ({len(visits)}):")
        for v in visits:
            state = "DELETED" if v["deleted_at"] else v["status"]
            when = "future" if v["future"] else "past"
            print(f"    visit {v['stride_id']}  case {v['case_stride_id']}  {v['starts_at_local']:%a %b %d %I:%M %p}"
                  f"  {state:<12} ({when}, last change {v['changed']} UTC)")
    print("  Last status changes:")
    for h in conn.execute(
            "select to_char(changed_at,'MM-DD HH24:MI') t,from_status,to_status,source,reason from lead_status_history "
            "where lead_id=%s order by changed_at desc limit 6", (lead["id"],)).fetchall():
        print(f"    {h['t']}  {h['from_status'] or '-'} → {h['to_status']}  ({h['source']}: {h['reason'] or ''})")
    print("  Last Sheet updates:")
    for o in conn.execute(
            "select to_char(created_at,'MM-DD HH24:MI') t,event_type,status from integration_outbox "
            "where aggregate_id=%s and destination='n8n' order by id desc limit 4", (str(lead["id"]),)).fetchall():
        print(f"    {o['t']}  {o['event_type'].removeprefix('sheet.')}  → {o['status']}")


def show_files(conn) -> None:
    print("Last Stride files our server loaded (UTC):")
    for f in conn.execute(
            "select file_name,status,row_count,applied_count,unchanged_count,issue_count,issues,"
            "to_char(processed_at,'MM-DD HH24:MI') t from stride_import_files order by id desc limit 12").fetchall():
        issues = ", ".join(f"row {i.get('row')}: {i.get('reason')}" for i in (f["issues"] or [])) or ""
        print(f"  {f['t']}  {f['file_name']:<38} {f['status']:<10} rows {f['row_count']}, changed "
              f"{f['applied_count']}, same {f['unchanged_count']}" + (f"  ISSUES: {issues}" if issues else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("lead", nargs="?", help="lead name (or part of it) or the first 8 characters of its id")
    parser.add_argument("--files", action="store_true", help="show the last Stride files instead")
    args = parser.parse_args()
    with psycopg.connect(get_settings().supabase_db_url, row_factory=dict_row) as conn:
        if args.files or not args.lead:
            show_files(conn)
            return
        if re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f-]+)?", args.lead):
            leads = conn.execute("select * from leads where id::text like %s", (args.lead + "%",)).fetchall()
        else:
            leads = conn.execute("select * from leads where full_name ilike %s order by created_at desc limit 5",
                                 (f"%{args.lead}%",)).fetchall()
        if not leads:
            print("No lead found. Try part of the name, or the first 8 characters of the Lead ID from the Sheet.")
        for lead in leads:
            show_lead(conn, lead)


if __name__ == "__main__":
    main()
