"""Direct Sheet sync under load: N pasted "Start cadence" rows through the real code and a real
(LOCAL, disposable) Postgres, with an in-memory Sheet. No provider is called: the cadence worker
does not run, so nobody is dialled or texted.

    TEST_DATABASE_URL=<local db> uv run --frozen python scripts/sheet_load_test.py --rows 10000

Checks every row runs exactly once and reports timings. Refuses any non-local database.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import psycopg
from fake_sheet import FakeSheet
from psycopg.rows import dict_row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=10_000)
    parser.add_argument("--per-poll", type=int, default=200)
    args = parser.parse_args()
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url or not any(h in url for h in ("host=/", "localhost", "127.0.0.1")):
        sys.exit("TEST_DATABASE_URL must point at a local, disposable database")
    slug = f"load-{uuid4().hex[:8]}"
    os.environ.update({
        "SUPABASE_DB_URL": url, "N8N_PRACTICE_SLUG": slug, "SHEET_SYNC_ENABLED": "true",
        "SHEET_SYNC_MODE": "direct", "DASHBOARD_PUBLIC_URL": "https://dash.example.test",
        "GOOGLE_SHEET_ID": "load", "SHEET_SETTLE_SECONDS": "0",
        "SHEET_MAX_COMMANDS_PER_POLL": str(args.per_poll), "DB_POOL_MAX_SIZE": "5",
    })
    from rpt_agent import sheet_sync_worker
    from rpt_agent.observability import WorkflowTrace
    from rpt_agent.services import sheet_direct
    from rpt_agent.services.google_sheet import parse_grid

    conn = psycopg.connect(url, row_factory=dict_row, autocommit=True)
    practice = conn.execute("insert into practices(name,slug,timezone) values('Load',%s,"
                            "'America/Los_Angeles') returning id", (slug,)).fetchone()["id"]
    conn.execute("insert into practice_settings(practice_id) values(%s)", (practice,))
    version = conn.execute("insert into cadence_versions(practice_id,version_number,name,status,"
                           "activated_at) values(%s,1,'v1','active',now()) returning id",
                           (practice,)).fetchone()["id"]
    for order, (day, channel) in enumerate([(0, "call"), (0, "sms"), (3, "call"), (5, "sms")]):
        conn.execute("insert into cadence_steps(practice_id,cadence_version_id,step_order,day_offset,"
                     "channel,key,description,is_active) values(%s,%s,%s,%s,%s,%s,'s',true)",
                     (practice, version, order, day, channel, f"s{order}-{slug}"))
    conn.execute("update integration_outbox set status='dead' where destination='n8n' "
                 "and status in ('pending','sending')")
    sheet = FakeSheet()
    for i in range(args.rows):
        sheet.add(name=f"Load Patient {i}", phone=f"555{7_000_000 + i:07d}",
                  email=f"load{i}@example.test", date_of_birth="16/01/1990",
                  location="Dana Point", case="Ortho", action="Start cadence")
    report: dict[str, object] = {"rows": args.rows, "per_poll": args.per_poll}

    def tick(full: bool) -> float:
        started = time.perf_counter()
        sheet_sync_worker.run_direct_tick(sheet, full=full)
        return time.perf_counter() - started

    try:
        # 1. A pasted block of N new rows, worked through `per_poll` at a time.
        started, polls, slowest = time.perf_counter(), 0, 0.0
        while True:
            seconds = tick(full=False)
            polls += 1
            slowest = max(slowest, seconds)
            done = sum(1 for row in sheet.values[2:] if row[9] == "Cadence started")
            if done >= args.rows or polls > args.rows // args.per_poll + 20:
                break
        total = time.perf_counter() - started
        leads = conn.execute("select count(*) n, count(distinct phone_e164) phones from leads "
                             "where practice_id=%s", (practice,)).fetchone()
        requests = conn.execute("select count(*) n from lead_action_requests where practice_id=%s",
                                (str(practice),)).fetchone()["n"]
        ids = [row[16] for row in sheet.values[2:]]
        report["paste"] = {
            "polls": polls, "seconds_total": round(total, 1),
            "ms_per_row": round(1000 * total / args.rows, 1), "slowest_poll_s": round(slowest, 2),
            "rows_started": done, "leads_created": leads["n"], "unique_phones": leads["phones"],
            "backend_requests": requests, "distinct_lead_ids_in_sheet": len(set(ids)),
            "exactly_once": done == leads["n"] == leads["phones"] == requests == len(set(ids))
            == args.rows,
        }
        # 2. Quiet polls over N linked rows (nothing to do), and the outbox drained.
        for _ in range(5):
            tick(full=False)
        quiet = [tick(full=False) for _ in range(3)]
        full = [tick(full=True) for _ in range(2)]
        report["quiet"] = {"narrow_poll_s": round(max(quiet), 3), "full_poll_s": round(max(full), 3)}
        # 3. Staff edit 100 emails: sent after two full reads, once each.
        picked = random.sample(range(args.rows), min(100, args.rows))
        for i in picked:
            sheet.values[2 + i][2] = f"edited{i}@example.test"
        tick(full=True)
        tick(full=True)
        edited = conn.execute("select count(*) n from leads where practice_id=%s and email like "
                              "'edited%%'", (practice,)).fetchone()["n"]
        report["profile_edits"] = {"edited": len(picked), "synced": edited}
        # 4. Half the leads finished 40 days ago: archive them.
        conn.execute("update leads set status='booked',cadence_state='completed',"
                     "status_changed_at=now()-interval '40 days' where id in (select id from leads "
                     "where practice_id=%s order by created_at limit %s)", (practice, args.rows // 2))
        grid = parse_grid(sheet.read_values(), 2)
        started = time.perf_counter()
        moved = sheet_direct.archive(sheet, grid, 30, "Archive", WorkflowTrace("load", "load"))
        report["archive"] = {"moved": moved, "seconds": round(time.perf_counter() - started, 2),
                             "left_in_tab": len(sheet.values) - 2}
    finally:
        ids = [r["id"] for r in conn.execute("select id::text as id from leads where practice_id=%s",
                                             (practice,)).fetchall()]
        conn.execute("delete from integration_outbox where aggregate_id=any(%s)", (ids,))
        conn.execute("delete from lead_action_requests where practice_id=%s", (str(practice),))
        conn.execute("delete from leads where practice_id=%s", (practice,))
        conn.execute("delete from practices where id=%s", (practice,))
        conn.close()
    import json

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
