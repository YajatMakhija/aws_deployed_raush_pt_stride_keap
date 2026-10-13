# Phase 2 — Stride → our database (Stride SFTP import)

Status (2026-10-08): built, tested locally, deployed **switched off** (`STRIDE_IMPORT_ENABLED=false`).
Nothing reads Stride files in production until we turn it on. Keap sync (phase 2B) is not built yet.

## 1. Why

Stride cannot be queried for patients or appointments. Instead Stride uploads CSV files to our SFTP
server: one full export of all Rausch PT data, then every ~5 minutes only the rows that changed. We
copy those files into our database so that:

- leads we are calling who book on their own (SMS link, phone, front desk) are marked **booked** and
  their calls/texts stop;
- a later Keap sync (phase 2B) has one clean, up-to-date local copy to read from.

Booking through the Stride API stays disabled in this phase.

## 2. How it works

```text
Stride ──SFTP──> /srv/sftp/stride/incoming  (EC2, chrooted user "stride", key-only)
                         │  every 60 s
                         ▼
                 stride-worker (new container)
                   1. pick finished files (unchanged 60 s; temp names ignored), oldest first
                   2. load each file in ONE database transaction (all or nothing)
                   3. move it to /srv/stride/archive (loaded) or /srv/stride/error (not loadable)
                   4. link Stride patients to our leads; mark booked / flag cancellations
                   5. write a heartbeat for the dashboard watchdog
                         │
                         ▼
                 Supabase: stride_* tables  ──(phase 2B, later)──> Keap
```

Files: `<type>_<YYYYMMDDHHMMSS>.csv`, types `patients`, `patient_cases`, `appointments`, `users`,
`locations`, `notes`, `patient_insurances`, `payers`, `providers`. Every row has
`Action = Upsert | Delete` and a `Modified Date Time`.

## 3. Rules the import follows

| Situation | What happens |
| --- | --- |
| New record (Upsert, unknown id) | Inserted |
| Changed record (Upsert, newer Modified Date Time) | Updated |
| Older change arriving late | Ignored — an older row never overwrites a newer one |
| Same row re-sent unchanged | Not counted as a change (`updated_at` does not move) |
| Same file sent twice (name + checksum) | Skipped, archived |
| Delete | Row kept, `deleted_at` set (tombstone). Keap is never told to delete |
| Appointment Delete with no Appointment Id | Matched by case + local start time (Stride allows one appointment per case per start), narrowed by type/clinician/location. 0 or 2+ matches → reported, never guessed |
| Child before parent (appointment before its patient) | Kept; no foreign keys between stride tables |
| Bad row (bad date, wrong column count, no id) | Skipped and reported (row number + reason, no patient data) |
| More than 1% bad rows | Whole file rejected → error folder ⚠️ see open decision 1 |
| Missing required column, unreadable, unknown name | Whole file rejected → error folder |
| File over `STRIDE_MAX_FILE_MB` (2 GB) | Rejected unread |
| Database/network failure | Nothing saved, file stays, retried; after 3 attempts → error folder |
| A file is being retried | Later files wait behind it (strict order) |
| Importer killed mid-file (e.g. out of memory) | The attempt was recorded before work started, so it still counts |
| Very large file | Streamed: memory stays ~50 MB. 1,000,000 rows (228 MB) loaded in 34 s locally |
| New column added by Stride | Stored automatically in `raw` |

Appointment Date/Start/End are clinic-local times (`practice_settings.stride_location_timezone`,
America/New_York); UTC is derived. `Modified Date Time` is treated as UTC (only used for ordering).

## 4. Leads (Sheet / dashboard) and Stride patients

Recomputed every tick, so a crash or re-run cannot leave it half done.

- **Link**: a Stride patient belongs to a lead when the lead already has that `stride_patient_id`, or
  the normalized full name matches AND (same date of birth, or — only if either DOB is missing — the
  lead's phone is one of the patient's phones). Two possible leads → `ambiguous`, left for staff.
- **Booked**: a linked lead with a future, non-cancelled appointment → `status=booked`,
  `cadence_state=completed`, planned calls/texts skipped, history `source=stride`, Google Sheet updated.
- **Cancelled**: if that booking (an appointment starting after the lead was booked) is cancelled or
  deleted with no replacement → `needs_attention` + `needs_review` ("Stride appointment cancelled").
  The lead is **not** put back into calls/texts.
- Walk-in patients (no lead) never touch `leads`.

## 5. Database changes — migration `032_stride_import.sql`

Applied to Supabase on 2026-10-08. All new tables have RLS on and no policies (backend only).
Nothing in existing tables changed except one new index on `leads`.

| Table | One row per | Key columns |
| --- | --- | --- |
| `stride_import_files` | file received | `file_name`, `entity`, `sha256`, `size_bytes`, `status` (pending / processed / retrying / rejected), `attempts`, `row_count`, `applied_count`, `unchanged_count`, `issue_count`, `issues` (PHI-free), `error`, `received_at`, `processed_at` |
| `stride_patients` | patient | `stride_id`, names, `date_of_birth`, gender, emails, phones (E.164), address, `marketing_opt_in`, `first/latest_appointment_at`, `lead_id`, `lead_match_status` (unmatched / matched / ambiguous) |
| `stride_cases` | case | `stride_id`, `patient_stride_id`, `title`, `status`, referring provider, `last_appointment_at` |
| `stride_appointments` | appointment | `stride_id`, patient, case, `appointment_type_id/name`, `status`, `starts_at_local`, `ends_at_local`, `start_utc`, `end_utc`, location, clinician id/name, place of service, payment type |
| `stride_users` | clinician | `stride_id`, names, email, credentials, NPI, `is_active` |
| `stride_locations` | location | `stride_id`, name, address, phone, `is_active` |
| `stride_other_records` | note / insurance / payer / referring provider | `entity`, `stride_id`, `patient_stride_id` — full row in `raw` only (not used yet) |
| `service_heartbeats` | background service | `service`, `last_seen_at`, `details` (files waiting, oldest wait, disk free, progress) |

Shared by every `stride_*` table: primary key `(practice_id, stride_id)`, `raw` (the full CSV row, so
a field we ignore today can be used later without a schema change or re-import), `stride_created_at`,
`stride_modified_at`, `deleted_at`, `import_file_id`, `created_at`, `updated_at` (moves only on a real
change — the cursor phase 2B will use).

New index: `idx_leads_match_name` on `leads(practice_id, lower(last_name), date_of_birth)`.

## 6. Watchdog and dashboard endpoint

`GET /api/v1/dashboard/stride-sync` (dashboard auth) returns, in plain words:

- `overall`: ok / attention / problem / not_started, and a one-line `headline`;
- `alerts`: import stopped (no heartbeat for 5 min), file not loaded, file retrying, files waiting
  over 15 min, no file for 30 min during clinic hours, SFTP disk under 20% / 10%, rows skipped today,
  patients matching more than one lead;
- `today`: files received, new/updated patients, new/cancelled appointments, check-ins, leads marked
  booked, bookings cancelled, rows skipped;
- `last_7_days` and `recent_files`.

The frontend page is not built yet (to be designed in `rpt_frontend` style).

## 7. Code

| File | Purpose |
| --- | --- |
| `supabase/migrations/032_stride_import.sql` | Tables above |
| `src/rpt_agent/services/stride_import.py` | Parse + apply one file (streaming COPY into a temp table, set-based merge) |
| `src/rpt_agent/services/stride_leads.py` | Link patients to leads, booked / cancelled rules |
| `src/rpt_agent/services/stride_sync_status.py` | Watchdog status for the dashboard |
| `src/rpt_agent/stride_import_worker.py` | `rpt-stride-worker` loop: pick files, attempts, move, heartbeat |
| `src/rpt_agent/routes/dashboard.py` | `GET /stride-sync` |
| `docker-compose.prod.yml` | `stride-worker` service: SFTP folders mounted, `mem_limit: 512m` |
| `scripts/fake_stride.py` | Local fake SFTP server + fake Stride feed (synthetic data) |
| `tests/test_stride_import*.py` | 68 tests (parsing, every edge case, leads, watchdog) |

Settings (`.env`, defaults shown): `STRIDE_IMPORT_ENABLED=false`, `STRIDE_PRACTICE_SLUG=rausch-pt`,
`STRIDE_IMPORT_POLL_SECONDS=60`, `STRIDE_FILE_SETTLE_SECONDS=60`, `STRIDE_IMPORT_MAX_ATTEMPTS=3`,
`STRIDE_MAX_FILE_MB=2048`, `STRIDE_MAX_BAD_ROW_PERCENT=1`. Folder paths are set by the compose file.

## 8. Server changes (EC2)

- `/etc/cron.d/stride-sftp-cleanup` now deletes only `/srv/stride/archive` and `/srv/stride/error`
  files older than 30 days. It used to delete unread inbox files after 2 days. Backup in `/root/`.
- `/srv/stride/archive` and `/srv/stride/error` created (root only, outside Stride's SFTP view).
- Unchanged: SFTP user `stride`, chroot `/srv/sftp/stride`, inbox `/incoming`, key-only login.

## 9. Testing done

- 68 new automated tests on a real Postgres schema (all 297 backend tests pass).
- Real sample CSVs from Stride (9 files) imported; the old `perform_demo_*` file correctly rejected.
- Live run over a local fake SFTP server: full load + 7 rounds of changes (new bookings, phone
  changes, reschedules, cancels, check-ins, deletes without id, re-sent file, late older file, bad rows,
  missing column). Sheet leads were booked, a cancelled one was flagged, walk-ins left leads alone.
- 1,000,000-row file: 49 MB peak memory.

## 10. Before turning it on

1. From Stride: SSH public key, source IPs, a test drop, answers to our questions (email sent).
2. Add Stride's key to `/home/stride/.ssh/authorized_keys`; narrow the AWS security group for port 22.
3. Decide storage (recommended: separate ~30 GB EBS volume, about $3/month).
4. Set `STRIDE_IMPORT_ENABLED=true` in the server `.env`, then
   `docker compose -f docker-compose.prod.yml up -d stride-worker`.
5. Watch the first full load (`docker compose -f docker-compose.prod.yml logs -f stride-worker`) and
   `GET /api/v1/dashboard/stride-sync`.

## 11. Open decisions

1. **1% bad-row rule on tiny files**: Stride's 5-minute files have a few rows, so one bad row rejects
   the whole file (good rows too). Suggested: reject only when 10+ rows AND more than 1% are bad.
2. Store notes / insurance files at all (sensitive, not used yet)? Waiting on senior.
3. Storage option (separate disk vs today).
4. Dashboard page design (in `rpt_frontend` style, Salesforce-like flags).
5. Phase 2A/2B (Keap): built and tested against the sandbox (2026-10-14); see `PHASE_2A_KEAP_DATA.md` /
   `PHASE_2B_KEAP_DELIVERY.md`. Waiting on the real Rausch appointment types and Michelle's confirmations.

## 12. Run it locally

```bash
# a disposable Postgres (no Docker needed): see tests/CLAUDE.md, then
export SUPABASE_DB_URL=<local url> TEST_DATABASE_URL=<local url>
~/.local/bin/uv run --frozen rpt migrate
~/.local/bin/uv run --frozen python -m pytest -q tests/test_stride_import*.py
# fake Stride over SFTP
~/.local/bin/uv run --with asyncssh python scripts/fake_stride.py server
STRIDE_IMPORT_ENABLED=true STRIDE_INBOX_DIR=local/stride/incoming STRIDE_ARCHIVE_DIR=local/stride/archive \
  STRIDE_ERROR_DIR=local/stride/error ~/.local/bin/uv run --frozen rpt-stride-worker
~/.local/bin/uv run --with asyncssh python scripts/fake_stride.py feed --every 300
```
