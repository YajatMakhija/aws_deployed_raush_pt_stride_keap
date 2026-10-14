"""Show what our server planned for a person in Keap AND what the Keap contact really holds (read-only).

    uv run --frozen python scripts/keap_check.py "Lead name"        # part of the name
    uv run --frozen python scripts/keap_check.py 4e9b6bf1            # first 8 characters of the lead id
    uv run --frozen python scripts/keap_check.py --patient 900036    # a Stride-only patient (walk-in)
    uv run --frozen python scripts/keap_check.py --status            # the Keap sync status (like the dashboard)

Uses the database in .env (demo) and the saved Keap connection. Never writes to Keap or the database.
Names are shortened to first letters; tag ids are shown with their names.
"""

from __future__ import annotations

import argparse
import re
import uuid

import httpx
import psycopg
from psycopg.rows import dict_row

from rpt_agent.config import get_settings
from rpt_agent.observability import WorkflowTrace
from rpt_agent.services.keap_client import KeapClient

# Sandbox (puq284) tag ids from docs/PHASE_2A_KEAP_DATA.md; unknown ids are printed as numbers.
TAG_NAMES = {"144": "Pending", "186": "AI Gave Up", "180": "New Eval Booked", "172": "Eval Completed",
             "184": "PT Appt Booked", "176": "PT Appt Completed", "174": "POC Appt Booked",
             "156": "POC Completed"}


def names(tag_ids) -> list[str]:
    return [TAG_NAMES.get(str(t), str(t)) for t in tag_ids or []]


def short(text: str | None) -> str:
    return " ".join(part[:1] + "***" for part in (text or "?").split())


def show_row(conn, row: dict, practice_id: int) -> None:
    lead = conn.execute("select full_name,status from leads where id=%s", (row["lead_id"],)).fetchone() \
        if row["lead_id"] else None
    who = f"lead {str(row['lead_id'])[:8]} {short(lead['full_name'])} ({lead['status']})" if lead \
        else f"Stride patient {row['stride_patient_id']} (no lead)"
    print("=" * 72)
    print(who)
    print(f"  Our plan:  sync={row['status']}  wanted tags={names((row['desired'] or {}).get('tags'))}"
          + (f"  skipped: {row['skip_reason']}" if row["skip_reason"] else "")
          + (f"  error: {row['last_error']}" if row["last_error"] else ""))
    if not row["keap_contact_id"]:
        print("  In Keap:   no contact (not sent yet, or kept out on purpose)")
        return
    client = KeapClient(practice_id, get_settings(), httpx.Client(timeout=30), audit_enabled=False)
    contact = client.get_contact(WorkflowTrace("keap_check", "cli", uuid.uuid4().hex), row["keap_contact_id"])
    if not contact:
        print(f"  In Keap:   contact {row['keap_contact_id']} not found")
        return
    fields = {str(f.get("id")): f.get("content") for f in contact.get("custom_fields") or []}
    print(f"  In Keap:   contact {row['keap_contact_id']} {short(contact.get('given_name'))} "
          f"{short(contact.get('family_name'))}  tags={names(contact.get('tag_ids'))}")
    print(f"             date={fields.get('6')}  time={fields.get('8')}  clinician={fields.get('10')}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("lead", nargs="?", help="part of the lead name, or the first 8 characters of its id")
    parser.add_argument("--patient", type=int, help="a Stride patient id (walk-in, no lead)")
    parser.add_argument("--status", action="store_true", help="show the overall Keap sync status")
    args = parser.parse_args()
    with psycopg.connect(get_settings().supabase_db_url, row_factory=dict_row) as conn:
        practice_id = conn.execute("select id from practices where slug=%s",
                                   (get_settings().stride_practice_slug,)).fetchone()["id"]
        if args.status or not (args.lead or args.patient):
            connection = conn.execute("select status,keap_account,access_token_expires_at from keap_connections "
                                      "where practice_id=%s", (practice_id,)).fetchone()
            counts = conn.execute("select status,count(*) n from keap_sync where practice_id=%s group by status",
                                  (practice_id,)).fetchall()
            print(f"Connection: {dict(connection) if connection else 'not connected'}")
            print("Rows: " + (", ".join(f"{c['status']}={c['n']}" for c in counts) or "none yet"))
            return
        if args.patient:
            rows = conn.execute("select * from keap_sync where practice_id=%s and stride_patient_id=%s",
                                (practice_id, args.patient)).fetchall()
        elif re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f-]+)?", args.lead):
            rows = conn.execute("select * from keap_sync where lead_id::text like %s", (args.lead + "%",)).fetchall()
        else:
            rows = conn.execute("select k.* from keap_sync k join leads l on l.id=k.lead_id "
                                "where l.full_name ilike %s order by k.id desc limit 5",
                                (f"%{args.lead}%",)).fetchall()
        if not rows:
            print("No Keap row yet. A lead gets one within a minute of being added (if created after the Keap start "
                  "date); a Do Not Contact lead is kept out on purpose.")
        for row in rows:
            show_row(conn, row, practice_id)


if __name__ == "__main__":
    main()
