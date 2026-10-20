"""Local stand-in for Stride's SFTP delivery. Development only, synthetic data only.

    # terminal 1: SFTP server on 127.0.0.1:2222, user "stride", key auth, jailed to local/stride/incoming
    ~/.local/bin/uv run --with asyncssh python scripts/fake_stride.py server

    # terminal 2: upload a full load, then a delta every 5 minutes, like Stride will
    ~/.local/bin/uv run --with asyncssh python scripts/fake_stride.py feed --every 300

    # optional: add synthetic Sheet leads (local database only) whose names the feed books
    ~/.local/bin/uv run --frozen python scripts/fake_stride.py seed-leads

Files use Stride's exact names and columns. Each delta exercises one or more real-life
changes: new patients and bookings, phone changes, reschedules, cancellations,
check-ins, Deletes without an Appointment Id, a re-sent file, a late older file, a bad
row and (sometimes) a file with a missing column. All phones are 555-01xx (fictional).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / "local"
INBOX = LOCAL / "stride" / "incoming"
KEYS = LOCAL / "sftp"
STATE = LOCAL / "fake_stride_state.json"
PORT = 2222

HEADERS = {
    "locations": "Id,Name,Address 1,Address 2,City,State,Zip Code,Phone Number,Fax Number,"
    "Created Date Time,Modified Date Time,Action,Is Active",
    "users": "Id,First Name,Last Name,Email,Credentials,License,NPI,Created Date Time,Modified Date Time,"
    "Active,Action",
    "patients": "Id,External Id,First Name,Last Name,Date Of Birth,Address 1,Address 2,City,State,Zip,"
    "Marital Status,Gender,Occupation,First Appointment,Latest Appointment,Personal Email,Work Email,"
    "Preferred Email,Mobile Phone Number,Home Phone Number,Work Phone Number,Preferred Phone Number,"
    "Emergency Contact Name,Emergency Contact Relationship,Emergency Contact Phone Number,Referral Source,"
    "Referral Details,Primary Care Provider Id,Primary Care Provider Name,Primary Care Provider NPI,"
    "Created Date Time,Modified Date Time,Action,Marketing Opt-in",
    "patient_cases": "Patient Id,Patient Name,Patient Date Of Birth,Id,Title,Details,Last Appointment,Status,"
    "Referring Provider Id,Referring Provider Name,Referring Provider NPI,Created Date Time,"
    "Modified Date Time,Action",
    "appointments": "Patient Id,Patient Name,Patient Date Of Birth,Case Id,Case Title,Appointment Id,"
    "Category,Appointment Type,Appointment Type Id,Subject,Payment Type,Date,Start Time,End Time,Status,"
    "Place of Service,Location Id,Location Name,Primary Attendee Id,Primary Attendee Name,Details,"
    "Created Date Time,Modified Date Time,Action",
}
TYPES = [("1", "PT"), ("2", "Initial Evaluation"), ("3", "Progress Note"),
         ("5", "PT Premium"), ("6", "Pelvic")]
FIRST = ["Alpha", "Bravo", "Cyra", "Dax", "Esme", "Finn", "Gia", "Hugo", "Ines", "Jory", "Kai", "Lena"]
LAST = ["Synth", "Mock", "Sample", "Demo"]
# Names the seed-leads command adds as Sheet leads; the feed books them later.
LEAD_PATIENTS = [("Quinn", "Synthlead", "1988-03-14"), ("Rory", "Synthlead", "1975-11-02"),
                 ("Sky", "Synthlead", "1992-07-21")]


def stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def to_csv(entity: str, rows: list[dict], drop_column: str | None = None) -> bytes:
    header = [c for c in HEADERS[entity].split(",") if c != drop_column]
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=header, lineterminator="\r\n", extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: row.get(name, "") for name in header})
    return out.getvalue().encode()


class Feed:
    def __init__(self) -> None:
        self.s = json.loads(STATE.read_text()) if STATE.exists() else {
            "next_patient": 50001, "next_case": 70001, "next_appt": 90001, "round": 0,
            "patients": {}, "appts": {}, "lead_index": 0, "last_files": [],
        }

    def save(self) -> None:
        STATE.write_text(json.dumps(self.s, indent=1))

    def _new_patient(self, now, first=None, last=None, dob=None) -> dict:
        pid = self.s["next_patient"]
        self.s["next_patient"] += 1
        n = pid % 100
        row = {
            "Id": pid, "First Name": first or random.choice(FIRST), "Last Name": last or random.choice(LAST),
            "Date Of Birth": dob or f"19{random.randint(50, 99)}-{random.randint(1, 12):02d}-"
            f"{random.randint(1, 28):02d}",
            "Address 1": f"{n} Synthetic Way", "City": "Jupiter", "State": "FL", "Zip": "33458",
            "Gender": random.choice(["M", "F", ""]),
            "Personal Email": f"synthetic{pid}@example.test" if random.random() < 0.6 else "",
            "Mobile Phone Number": f"(561) 555-01{n % 100:02d}" if random.random() < 0.8 else "",
            "Preferred Phone Number": "M", "Marketing Opt-in": "True",
            "Created Date Time": stamp(now), "Modified Date Time": stamp(now), "Action": "Upsert",
        }
        row["Preferred Email"] = "P" if row["Personal Email"] else ""
        self.s["patients"][str(pid)] = row
        return row

    def _case(self, patient: dict, now) -> dict:
        cid = self.s["next_case"]
        self.s["next_case"] += 1
        return {"Patient Id": patient["Id"], "Id": cid, "Title": "Synthetic case", "Status": "A",
                "Created Date Time": stamp(now), "Modified Date Time": stamp(now), "Action": "Upsert"}

    def _appt(self, patient: dict, case_id: int, now, days_ahead: int, type_index=1) -> dict:
        aid = self.s["next_appt"]
        self.s["next_appt"] += 1
        day = (now + timedelta(days=days_ahead)).date().isoformat()
        hour = random.choice([8, 9, 10, 11, 13, 14, 15, 16])
        type_id, type_name = TYPES[type_index]
        row = {
            "Patient Id": patient["Id"], "Case Id": case_id, "Appointment Id": aid,
            "Category": "Patient Appointment", "Appointment Type": type_name, "Appointment Type Id": type_id,
            "Payment Type": "Commercial", "Date": day,
            "Start Time": f"{(hour - 1) % 12 + 1}:00 {'AM' if hour < 12 else 'PM'}",
            "End Time": f"{hour % 12 + 1}:00 {'AM' if hour + 1 < 12 else 'PM'}",
            "Status": "Unmarked", "Place of Service": "11 - Office", "Location Id": 2,
            "Location Name": "Synthetic Clinic", "Primary Attendee Id": 3, "Primary Attendee Name": "Clinician Synth",
            "Created Date Time": stamp(now), "Modified Date Time": stamp(now), "Action": "Upsert",
        }
        self.s["appts"][str(aid)] = row
        return row

    def full_load(self, now) -> dict[str, list[dict]]:
        files = {
            "locations": [{"Id": 2, "Name": "Synthetic Clinic", "City": "Jupiter", "State": "FL",
                           "Created Date Time": stamp(now), "Modified Date Time": stamp(now),
                           "Action": "Upsert", "Is Active": "True"}],
            "users": [{"Id": 3, "First Name": "Clinician", "Last Name": "Synth", "Credentials": "PT",
                       "Created Date Time": stamp(now), "Modified Date Time": stamp(now), "Active": "TRUE",
                       "Action": "Upsert"}],
            "patients": [], "patient_cases": [], "appointments": [],
        }
        for _ in range(40):
            patient = self._new_patient(now)
            case = self._case(patient, now)
            files["patients"].append(patient)
            files["patient_cases"].append(case)
            for _ in range(random.randint(0, 3)):
                files["appointments"].append(self._appt(patient, case["Id"], now, random.randint(-60, 30),
                                                        random.randint(0, 2)))
        return files

    def delta(self, now) -> tuple[dict[str, list[dict]], list[str]]:
        files: dict[str, list[dict]] = {"patients": [], "patient_cases": [], "appointments": []}
        notes = []
        r = self.s["round"]
        # New walk-in patients who book.
        for _ in range(random.randint(1, 3)):
            patient = self._new_patient(now)
            case = self._case(patient, now)
            files["patients"].append(patient)
            files["patient_cases"].append(case)
            files["appointments"].append(self._appt(patient, case["Id"], now, random.randint(1, 14), 1))
        notes.append("new patients booked")
        # One of the seeded Sheet leads books by themselves.
        if self.s["lead_index"] < len(LEAD_PATIENTS) and r % 2 == 0:
            first, last, dob = LEAD_PATIENTS[self.s["lead_index"]]
            self.s["lead_index"] += 1
            patient = self._new_patient(now, first, last, dob)
            case = self._case(patient, now)
            files["patients"].append(patient)
            files["patient_cases"].append(case)
            files["appointments"].append(self._appt(patient, case["Id"], now, 5, 1))
            notes.append(f"Sheet lead {first} {last} booked")
        future = [a for a in self.s["appts"].values()
                  if a["Action"] == "Upsert" and a["Status"] == "Unmarked"
                  and a["Date"] > now.date().isoformat()]
        past = [a for a in self.s["appts"].values()
                if a["Action"] == "Upsert" and a["Status"] == "Unmarked" and a["Date"] <= now.date().isoformat()]
        random.shuffle(future)
        if self.s["patients"]:
            p = self.s["patients"][random.choice(list(self.s["patients"]))]
            p.update({"Mobile Phone Number": f"(561) 555-01{random.randint(0, 99):02d}",
                      "Modified Date Time": stamp(now)})
            files["patients"].append(p)
            notes.append("phone changed")
        if future:
            a = future.pop()
            day = (datetime.fromisoformat(a["Date"]) + timedelta(days=2)).date().isoformat()
            a.update({"Date": day, "Modified Date Time": stamp(now)})
            files["appointments"].append(a)
            notes.append("rescheduled")
        if future:
            a = future.pop()
            a.update({"Status": "Cancel", "Modified Date Time": stamp(now)})
            files["appointments"].append(a)
            notes.append("cancelled")
        if past:
            a = random.choice(past)
            a.update({"Status": "Checked In", "Modified Date Time": stamp(now)})
            files["appointments"].append(a)
            notes.append("checked in")
        if future and r % 2 == 1:
            a = future.pop()
            a["Action"] = "Delete"
            gone = {k: v for k, v in a.items() if k not in {"Appointment Id", "Created Date Time"}}
            gone["Modified Date Time"] = stamp(now)
            files["appointments"].append(gone)
            notes.append("deleted (no Appointment Id)")
        if r % 3 == 2:
            files["patients"].append({"Id": 99999, "First Name": "Bad", "Last Name": "Row",
                                      "Date Of Birth": "31/31/1990", "Modified Date Time": stamp(now),
                                      "Action": "Upsert"})
            notes.append("one bad row")
        return files, notes


async def _upload(files: dict[str, bytes]) -> None:
    import asyncssh

    async with asyncssh.connect(
        "127.0.0.1", PORT, username="stride", client_keys=[str(KEYS / "client_key")], known_hosts=None
    ) as conn, conn.start_sftp_client() as sftp:
        for name, content in files.items():
            async with sftp.open(name, "wb") as remote:
                await remote.write(content)


async def feed(every: int, rounds: int) -> None:
    f = Feed()
    count = 0
    while True:
        now = datetime.now(UTC).replace(microsecond=0)
        export = now.strftime("%Y%m%d%H%M%S")
        files: dict[str, bytes] = {}
        if f.s["round"] == 0:
            notes = ["full load"]
            for entity, rows in f.full_load(now).items():
                files[f"{entity}_{export}.csv"] = to_csv(entity, rows)
        else:
            rows_by_entity, notes = f.delta(now)
            for entity, rows in rows_by_entity.items():
                if rows:
                    files[f"{entity}_{export}.csv"] = to_csv(entity, rows)
            r = f.s["round"]
            if r % 4 == 0 and f.s["last_files"]:
                name = f.s["last_files"][0]
                files[name] = (LOCAL / "fake_stride_sent" / name).read_bytes()
                notes.append(f"re-sent {name}")
            if r % 5 == 0 and f.s["patients"]:
                # An hour-old copy of a patient with a stale phone: must not win.
                hour_ago = now - timedelta(hours=1)
                stale = {**next(iter(f.s["patients"].values())), "Mobile Phone Number": "(561) 555-0000",
                         "Modified Date Time": stamp(hour_ago)}
                files[f"patients_{hour_ago:%Y%m%d%H%M%S}.csv"] = to_csv("patients", [stale])
                notes.append("late older file")
            if r % 6 == 0:
                files[f"patients_{export[:-2]}59.csv"] = to_csv("patients", [], drop_column="Date Of Birth")
                notes.append("file with a missing column")
        sent = LOCAL / "fake_stride_sent"
        sent.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (sent / name).write_bytes(content)
        await _upload(files)
        f.s["last_files"] = [n for n in files if n.endswith(f"{export}.csv")]
        f.s["round"] += 1
        f.save()
        print(f"{now:%H:%M:%S} round {f.s['round'] - 1}: uploaded {len(files)} files ({'; '.join(notes)})",
              flush=True)
        count += 1
        if rounds and count >= rounds:
            return
        await asyncio.sleep(every)


async def server() -> None:
    import asyncssh

    KEYS.mkdir(parents=True, exist_ok=True)
    INBOX.mkdir(parents=True, exist_ok=True)
    for name in ("host_key", "client_key"):
        if not (KEYS / name).exists():
            key = asyncssh.generate_private_key("ssh-ed25519")
            key.write_private_key(str(KEYS / name))
            (KEYS / name).chmod(0o600)
            key.write_public_key(str(KEYS / f"{name}.pub"), "openssh")
    class Server(asyncssh.SSHServer):
        # Only the "stride" user exists, like the real server; key-only login.
        def begin_auth(self, username):
            if username != "stride":
                raise asyncssh.PermissionDenied("unknown user")
            return True

    await asyncssh.create_server(
        Server, "127.0.0.1", PORT, server_host_keys=[str(KEYS / "host_key")],
        authorized_client_keys=str(KEYS / "client_key.pub"),
        sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(INBOX).encode()),
        allow_scp=False,
    )
    print(f"fake Stride SFTP on 127.0.0.1:{PORT}, user stride, files land in {INBOX}", flush=True)
    await asyncio.Future()


def seed_leads() -> None:
    """Add synthetic Google Sheet leads to the LOCAL database only."""
    sys.path.insert(0, str(ROOT / "src"))
    from rpt_agent.config import get_settings
    from rpt_agent.db import transaction

    url = get_settings().supabase_db_url
    if "supabase" in url or "amazonaws" in url or "localhost" not in url and "host=/" not in url:
        raise SystemExit("refusing: SUPABASE_DB_URL is not a local database")
    with transaction() as conn:
        practice = conn.execute("select id from practices where slug='rausch-pt'").fetchone()["id"]
        for index, (first, last, dob) in enumerate(LEAD_PATIENTS):
            exists = conn.execute(
                "select 1 from leads where practice_id=%s and full_name=%s", (practice, f"{first} {last}")
            ).fetchone()
            if exists:
                continue
            lead = conn.execute(
                "insert into leads(practice_id,source_system,full_name,first_name,last_name,phone_e164,"
                "date_of_birth,status,cadence_state,is_test) values(%s,'google_sheets',%s,%s,%s,%s,%s,"
                "'in_progress','active',true) returning id",
                (practice, f"{first} {last}", first, last, f"+1561555019{index}", dob),
            ).fetchone()["id"]
            for attempt in (1, 2, 3):
                conn.execute(
                    "insert into outreach_events(lead_id,attempt_no,channel,status,scheduled_for) "
                    "values(%s,%s,'call','planned',now()+make_interval(days=>%s))",
                    (lead, attempt, attempt),
                )
            print(f"added Sheet lead {first} {last}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("server")
    feed_parser = sub.add_parser("feed")
    feed_parser.add_argument("--every", type=int, default=300, help="seconds between deltas")
    feed_parser.add_argument("--rounds", type=int, default=0, help="stop after N uploads (0 = forever)")
    sub.add_parser("seed-leads")
    args = parser.parse_args()
    os.chdir(ROOT)
    if args.command == "server":
        asyncio.run(server())
    elif args.command == "feed":
        asyncio.run(feed(args.every, args.rounds))
    else:
        seed_leads()


if __name__ == "__main__":
    main()
