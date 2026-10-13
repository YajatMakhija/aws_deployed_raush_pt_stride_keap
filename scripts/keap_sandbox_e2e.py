"""End-to-end check of the Keap sync against the Keap SANDBOX with synthetic people.

Runs the real keap-worker tick (plan -> send) against a LOCAL database, creating a few
fake leads and Stride rows (555 numbers, @example.test emails) and checking what appears
in Keap after each step. Refuses to run against anything but a local database.

    SUPABASE_DB_URL=postgresql://...@127.0.0.1:.../rpt_local KEAP_SYNC_ENABLED=true \
    KEAP_INCLUDE_TEST_LEADS=true KEAP_CLIENT_ID=... KEAP_CLIENT_SECRET=... \
    python scripts/keap_sandbox_e2e.py

Prerequisites: the migrations, `scripts/keap_import_tokens.py`, and
`supabase/dev/keap_sandbox_mapping.sql` applied. Contacts it creates are listed at the
end so they can be removed by hand in the sandbox.
"""

import json
import sys
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.rows import dict_row

from rpt_agent import keap_sync_worker as worker
from rpt_agent.config import get_settings
from rpt_agent.observability import WorkflowTrace
from rpt_agent.services.keap_client import KeapClient

RUN = uuid.uuid4().hex[:5]
checks: list[tuple[str, bool, str]] = []
contact_ids: set[str] = set()


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))


def main() -> int:
    settings = get_settings()
    host = psycopg.conninfo.conninfo_to_dict(settings.supabase_db_url).get("host", "")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print("refusing to run: SUPABASE_DB_URL is not a local database")
        return 2
    if not (settings.keap_sync_enabled and settings.keap_include_test_leads):
        print("set KEAP_SYNC_ENABLED=true and KEAP_INCLUDE_TEST_LEADS=true")
        return 2

    conn = psycopg.connect(settings.supabase_db_url, row_factory=dict_row, autocommit=True)
    practice = conn.execute("select id from practices where slug=%s",
                            (settings.stride_practice_slug,)).fetchone()["id"]
    client = KeapClient(practice, settings)
    trace = WorkflowTrace("keap_e2e", "cli")
    seq = [9000]

    def tick():
        for _ in range(3):  # plan -> send -> pick up anything re-queued
            worker.run_keap_sync_tick(client)

    def lead(first, last, email=None, phone=None, status="in_progress"):
        return conn.execute(
            "insert into leads(practice_id,source_system,full_name,first_name,last_name,email,"
            "phone_e164,status,cadence_state,is_test) values(%s,'google_sheets',%s,%s,%s,%s,%s,%s,"
            "'active',true) returning id",
            (practice, f"{first} {last}", first, last, email, phone, status)).fetchone()["id"]

    def patient(first, last, email, lead_id=None, phone="+15555550177"):
        seq[0] += 1
        conn.execute(
            "insert into stride_patients(practice_id,stride_id,first_name,last_name,preferred_email,"
            "mobile_phone,preferred_phone,lead_id,stride_created_at) "
            "values(%s,%s,%s,%s,%s,%s,%s,%s,now())",
            (practice, seq[0], first, last, email, phone, phone, lead_id))
        if lead_id:
            conn.execute("update leads set stride_patient_id=%s where id=%s", (seq[0], lead_id))
        return seq[0]

    def appt(pid, type_id, days, status="Unmarked", clinician="Dana Clinician"):
        seq[0] += 1
        start = datetime.now(UTC) + timedelta(days=days)
        conn.execute(
            "insert into stride_appointments(practice_id,stride_id,patient_stride_id,"
            "appointment_type_id,appointment_type,status,start_utc,starts_at_local,clinician_id,"
            "clinician_name) values(%s,%s,%s,%s,'x',%s,%s,%s,7,%s)",
            (practice, seq[0], pid, type_id, status, start,
             (start - timedelta(hours=4)).replace(tzinfo=None, microsecond=0), clinician))
        return seq[0]

    def contact_of(lead_id=None, pid=None):
        row = conn.execute(
            "select keap_contact_id,status,last_error from keap_sync where "
            + ("lead_id=%s" if lead_id else "practice_id=%s and stride_patient_id=%s"),
            (lead_id,) if lead_id else (practice, pid)).fetchone()
        if not row or not row["keap_contact_id"]:
            return None, row
        contact_ids.add(row["keap_contact_id"])
        return client.get_contact(trace, row["keap_contact_id"]), row

    def tags(contact):
        return set(contact["tag_ids"]) if contact else set()

    def field(contact, field_id):
        for f in contact["custom_fields"]:
            if str(f["id"]) == field_id:
                return f["content"]
        return None

    # --- Quinn: Sheet lead, then books, then is checked in ---
    q_email = f"quinn.{RUN}@example.test"
    q = lead("Quinn", f"E2e{RUN}", q_email, "+15555550141")
    tick()
    c, row = contact_of(q)
    check("Quinn: contact created and synced", c and row["status"] == "synced", str(row))
    check("Quinn: Pending only", tags(c) == {"144"}, str(tags(c)))
    check("Quinn: contact type is Lead", c and c["contact_type"] == "Lead")
    q_pid = patient("Quinn", f"E2e{RUN}", q_email, q)
    q_appt = appt(q_pid, 2, 3)
    tick()
    c, _ = contact_of(q)
    check("Quinn books: IE Booked, Pending gone", tags(c) == {"180"}, str(tags(c)))
    check("Quinn books: date/time/clinician set",
          field(c, "6") and field(c, "8") and field(c, "10") == "Dana Clinician")
    conn.execute("update stride_appointments set status='Checked In',updated_at=now() "
                 "where practice_id=%s and stride_id=%s", (practice, q_appt))
    tick()
    c, _ = contact_of(q)
    check("Quinn checked in: Completed added, Booked kept", tags(c) == {"180", "172"}, str(tags(c)))

    # --- Rory: lead books, then the booking is cancelled ---
    r = lead("Rory", f"E2e{RUN}", f"rory.{RUN}@example.test", "+15555550142")
    r_pid = patient("Rory", f"E2e{RUN}", f"rory.{RUN}@example.test", r)
    r_appt = appt(r_pid, 2, 4)
    tick()
    c, _ = contact_of(r)
    check("Rory books: IE Booked", tags(c) == {"180"}, str(tags(c)))
    conn.execute("update stride_appointments set status='Cancel',updated_at=now() "
                 "where practice_id=%s and stride_id=%s", (practice, r_appt))
    conn.execute("update leads set status='needs_attention' where id=%s", (r,))
    tick()
    c, _ = contact_of(r)
    check("Rory cancels: Booked removed, Pending back", tags(c) == {"144"}, str(tags(c)))
    check("Rory cancels: date cleared", field(c, "6") is None, str(field(c, "6")))

    # --- Sky: walk-in patient with a Follow-up, no lead ---
    s_pid = patient("Sky", f"E2e{RUN}", f"sky.{RUN}@example.test")
    appt(s_pid, 1, 5)
    tick()
    c, row = contact_of(pid=s_pid)
    check("Sky walk-in: contact with Follow-up Booked and no Pending", c and tags(c) == {"184"},
          str(tags(c)))

    # --- Sam & Sue share one email: two contacts ---
    shared = f"family.{RUN}@example.test"
    sam = lead("Sam", f"E2e{RUN}", shared, "+15555550143")
    sue = lead("Sue", f"E2e{RUN}", shared, "+15555550144")
    tick()
    (c1, _), (c2, _) = contact_of(sam), contact_of(sue)
    check("Sam & Sue: two different contacts", c1 and c2 and c1["id"] != c2["id"])

    # --- Pre-existing contact made by hand, with a field of the clinic's own ---
    e_email = f"existing.{RUN}@example.test"
    made = client.create_contact(trace, {
        "given_name": "Ellis", "family_name": f"E2e{RUN}", "contact_type": "Customer",
        "email_addresses": [{"email": e_email, "field": "EMAIL1"}],
        "custom_fields": [{"id": "4", "content": "clinic note"}]})
    contact_ids.add(str(made["id"]))
    e = lead("Ellis", f"E2e{RUN}", e_email, "+15555550145")
    tick()
    c, _ = contact_of(e)
    check("Existing contact reused, not duplicated", c and str(c["id"]) == str(made["id"]))
    check("Existing contact: the clinic's own field kept", c and field(c, "4") == "clinic note")
    check("Existing contact: its type is not overwritten", c and c["contact_type"] == "Customer")
    check("Existing contact: Pending applied", c and "144" in tags(c))

    # --- Phone change ---
    conn.execute("update leads set phone_e164='+15555550199',updated_at=now() where id=%s", (q,))
    tick()
    c, _ = contact_of(q)
    check("Phone change: PHONE1 updated",
          c and any(p["field"] == "PHONE1" and p["number"] == "+15555550199"
                    for p in c["phone_numbers"]))

    # --- Deleted in Stride: nothing changes in Keap ---
    before, _ = contact_of(pid=s_pid)
    conn.execute("update stride_patients set deleted_at=now(),updated_at=now() "
                 "where practice_id=%s and stride_id=%s", (practice, s_pid))
    tick()
    after, _ = contact_of(pid=s_pid)
    check("Deleted in Stride: Keap unchanged", before and after and tags(before) == tags(after)
          and field(before, "6") == field(after, "6"))

    # --- Idempotence: another tick makes no writes ---
    used = client.limiter.used_today
    tick()
    created_before = client.stats["created"]
    check("Idle tick creates nothing", client.stats["created"] == created_before)
    print(json.dumps({"requests_used_this_run": client.limiter.used_today - 0, "before_idle": used}))

    failed = [c for c in checks if not c[1]]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    print("Keap sandbox contacts created in this run (remove by hand when done):",
          sorted(contact_ids, key=int))
    conn.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
