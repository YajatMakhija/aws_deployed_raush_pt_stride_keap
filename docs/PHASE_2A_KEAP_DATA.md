# Phase 2A — Decide what Keap should hold (desired state)

> **Prompt to start (paste into Claude Code / ChatGPT with this repo open):**
> "Implement Phase 2A exactly as described in `docs/PHASE_2A_KEAP_DATA.md`. Read `CLAUDE.md`,
> `context/PROJECT_CONTEXT.md`, `docs/PHASE2.md` and this file first. Follow the repo conventions. Build the
> migration, the service, the CLI command and every test listed in section 9. Do not call Keap — Phase 2A
> makes no HTTP requests. Run the full test suite and ruff, then open a PR.
> **Do section 0 first and stop there until the developer has answered every question in it.**"

Phase 2B (`docs/PHASE_2B_KEAP_DELIVERY.md`) sends what this phase decides. The two share one table
(`keap_sync`, section 4). 2A writes `desired`; 2B reads it and writes `synced`. Build 2A first.

---

## 1. Goal in one paragraph

For every person Rausch PT should have in Keap — every Google Sheet / dashboard lead and every Stride
patient — compute, from our own database only, exactly what their Keap contact should look like: name,
email, phone, birthday, which tags (Pending, Booked per appointment type, Completed per appointment type)
and which custom fields (appointment date, time, clinician per appointment type). Store it as one JSON
document per person with a hash. When the hash changes, the row is queued for Phase 2B. Nothing here talks
to Keap.

## 2. Background you need (read once)

- **Our system**: Python 3.12 FastAPI backend (`src/rpt_agent`), raw SQL with psycopg (`db.transaction()`),
  no ORM. Migrations in `supabase/migrations/NNN_name.sql` (next free number: **036**; never edit an
  applied one). Logging with `WorkflowTrace` (`observability.py`), **never log names, phones, emails, DOB**.
- **Leads** (`leads`): created by the Google Sheet (n8n → `services/lead_actions.py::_start_cadence`,
  `source_system='google_sheets'`) and by the dashboard (`routes/dashboard.py::create_dashboard_lead`).
  Useful columns: `id uuid`, `practice_id`, `full_name`, `first_name`, `last_name`, `email`, `phone_e164`,
  `date_of_birth`, `status`, `is_test`, `stride_patient_id`, `created_at`, `updated_at` (trigger-maintained).
  A lead becomes `status='booked'` through four paths (voice agent `voice_booking`, Sheet `n8n_sheet`, old
  booking API `stride_booking`, Stride import `stride`); 2A must not care which — it reads current state.
- **Stride mirror** (Phase 1, migration 032, see `docs/PHASE2.md`): `stride_patients`,
  `stride_appointments`, `stride_users`, … keyed `(practice_id, stride_id)`, with `deleted_at` tombstones
  and an `updated_at` that moves only on a real change. `stride_patients.lead_id` links a patient to a
  lead (set by `services/stride_leads.py`). Appointment columns you use: `patient_stride_id`,
  `appointment_type_id`, `appointment_type`, `status` (words: Unmarked, Confirmed, Checked In, No Show,
  Cancel, Late Cancel; API letters U/O/C/N/A/L), `start_utc`, `starts_at_local`, `clinician_id`,
  `clinician_name`, `deleted_at`, `updated_at`. `services/stride_import.py::is_cancelled()` already knows
  the cancelled statuses — reuse it.
- **Environments**: demo Supabase (the DB in `.env`, what the live server uses today) and the client's
  production Supabase. Everything goes to **demo first**. Never run a local worker against `.env` — it is a
  live DB. Tests use a disposable local Postgres (section 9.1).

## 0. First task: look at the real Keap tags, then agree how each case is tagged

The Keap admin (Michelle) has created the tags and custom fields, but how they map to our cases is not
settled yet. The rules in section 3 are a **draft**. Before writing any code:

**0.1 Fetch what exists (read-only, sandbox `puq284` first, then production).** The developer gives you
Keap API access (a Personal Access Token / Service Account Key, header `X-Keap-API-Key`, or an OAuth
token). Write a small throwaway script (not committed, token from an env var, never printed) that calls
only `GET` endpoints:

| What | Call |
| --- | --- |
| All tags (id, name, category) | `GET /rest/v2/tags?page_size=1000` (follow `next_page_token`) |
| Tag categories | `GET /rest/v2/tags/categories` |
| Contact custom fields (id, label, field type, options) | `GET /rest/v2/contacts/model` |
| Contact types / lead sources (if listed in the model) | same response |

Show the developer a table: tag id, tag name, category; custom field id, label, type. Point out anything
that looks like ours (Pending, Lead, Booked/Completed per appointment type, appointment date/time/clinician
per type) and anything unclear (duplicates, typos, a type with no Completed tag, etc.).
Never create, edit, apply or remove anything in Keap during this step.

**0.2 Ask the developer, one question at a time, how each case must look in Keap.** For every case, ask:
which tag is applied, which tag is removed, which custom fields are set or cleared, and **when** (on
contact creation or only at that moment). Cases, in the order they happen in our flow:

| # | Case (what happens in our system) | Where we see it |
| --- | --- | --- |
| C1 | New lead arrives from the Google Sheet / dashboard (cadence starts) | new `leads` row |
| C2 | Lead is still being called/texted (no booking yet) | `leads.status` not `booked` |
| C3 | Lead books (voice agent, Sheet "booked", or the clinic books in Stride) | `leads.status='booked'` and/or a future appointment in `stride_appointments` |
| C4 | Booked appointment is rescheduled (new date/time/clinician) | appointment `start_utc` / `clinician_id` changes |
| C5 | Booked appointment is cancelled or deleted in Stride | status Cancel / Late Cancel, or `deleted_at` set (our lead goes to `needs_attention`) |
| C6 | Patient re-books after a cancel | new non-cancelled appointment |
| C7 | Patient attends (appointment marked Checked In) → "completed" | appointment status `Checked In` |
| C8 | No Show | appointment status `No Show` |
| C9 | Patient books a second, different appointment type (e.g. Follow-up after Initial Evaluation) | another type in `stride_appointments` |
| C10 | Lead ends without booking: Do Not Contact, closed, or 14-day cadence finished | `leads.status` / `cadence_state` |
| C11 | Patient exists only in Stride (walk-in, never a Sheet lead) | `stride_patients` row with no `lead_id` |
| C12 | Patient already existed in Stride before go-live | `stride_patients.created_at` before `keap_sync_start_at` |
| C13 | Same person already exists in Keap (created by the clinic by hand) | email/phone match in Keap |

Also confirm: which appointment types count (by Stride type id), whether Booked stays after Completed,
and which contact type / lead source value to set.

**0.3 Write the answers into this file** — replace the draft rules in section 3, fill the mapping in the
migration seed (section 4: `keap_pending_tag_id`, `keap_appointment_types` rows with the real tag and
field ids, sandbox and production separately), update the test cases in section 9 and the open questions
in section 11. Then show the developer the changed sections and wait for "go" before building.

**Must stay in sync with our Stride flow (do not change it, read from it):**
- A lead is linked to a Stride patient by `services/stride_leads.py` (same name + DOB, or name + phone).
- That module marks the lead `booked` when a future non-cancelled appointment appears, and flags it
  `needs_attention` when that booking is cancelled/deleted. Keap tags must follow the **same** state, so
  Keap and our dashboard never disagree (e.g. Keap must not say "Booked" while the dashboard says
  "needs attention").
- Stride sends changes every 5 minutes; Keap may lag by one import tick plus the worker delay, never more.

## 0.4 Decisions so far (sandbox `puq284`, discovered 2026-10-13) — these override section 3 where they differ

**Scope for now:** the three mock Stride types only (`scripts/fake_stride.py` `TYPES`). Real Rausch types come later.

**Real Keap objects found (read-only):**
- Pending tag = `144`. Custom fields (single set, NOT per type): Appointment Date `6` (DATE), Appointment Time `8` (TEXT), Clinician `10` (TEXT).
- Draft type mapping (sandbox testing only; Michelle to confirm):

| Stride type (mock id) | Booked tag | Completed tag |
| --- | --- | --- |
| Initial Evaluation (2) | `180` STATUS: New Evaluation Booked | `172` STATUS: Eval Completed |
| Follow-up (1) | `184` STATUS: PT Appt Booked | `176` STATUS: PT Appt Completed |
| Progress Note (3) | `174` STATUS: POC Appt Booked | `156` STATUS: PT Completed their POC |

**Rule changes:**
- R8: only ONE date/time/clinician set. Value = the next upcoming counting appointment across ALL mapped types; none upcoming -> most recent past; none -> null. So `keap_appointment_types.date_field_id/time_field_id/clinician_field_id` are identical (`6/8/10`) on every row; `fields` in `desired` has exactly three entries.
- R2: `keap_include_existing_patients = true` for the sandbox (we want the full first load). `keap_sync_start_at = now() - 1 day`.
- R6: remove Booked when the appointment is cancelled/deleted (`keap_remove_booked_on_cancel = true`).
- R4 end of cadence: a lead whose cadence ends with no booking loses Pending and gets `186` STATUS: AI Gave Up (apply first, then remove Pending). Closed by staff -> proposal `168` Handed off to Human (not confirmed). **Do Not Contact: not handled yet** — Michelle decides the tag; until then a DNC lead is left unchanged. Do not delete contacts. Email opt-out via Keap's email-address-status API is a separate, later, verified step.
- C1 (new lead arrives): create contact (name, email, phone, birthday if known, contact type Lead), apply Pending `144`, leave date/time/clinician empty.
- Do not touch sandbox contacts 204 and 206.
- Test leads: `KEAP_INCLUDE_TEST_LEADS=true` in the sandbox only.

**2026-10-20, Keap admin's tag list:** one Booked (and where listed, Completed) tag per appointment type:
New Evaluation Booked; Re-Eval Booked; POC Appt Booked / PT Completed their POC; PT Premium Appt Booked /
Completed; Pelvic Appt Booked - Female / - Male and Pelvic Appt Completed. Pelvic is its own Stride type;
the Male tag goes in `keap_appointment_types.male_booked_tag_id` (migration 046) and is used when the
Stride patient's gender is `M`/`Male`. **For now Initial Evaluation, Re-Evaluation and Plan of Care (POC)
are not handled** (client, 2026-10-20): they are left out of the mapping, so New Evaluation Booked,
Re-Eval Booked, POC Appt Booked and PT Completed their POC are never applied.

**Still open:** C2-C13 walkthrough, exact `contact_type` string for Lead, whether DNC/Closed get tags, real Rausch appointment types.

## 3. Business rules (DRAFT — replace with the answers from section 0)

Source: the Keap admin's request — "Add contact to Keap if they aren't already in the system: first name,
last name, email, phone, birthday if we have it. Tag the patient when they book an appointment; each
appointment type has its own unique tag. Insert appointment date into a custom field; each appointment type
has its own date field. Nice to have: appointment time, who the appointment is with. Tag the patient when the
appointment is marked completed; each appointment type has its own completed tag."

| # | Rule |
| --- | --- |
| R1 | **Who gets a Keap contact**: every non-test lead, and every Stride patient that is not deleted and is in scope (R2). A lead linked to a Stride patient is **one person** (one row, one Keap contact). |
| R2 | **Stride patients in scope**: patients created in Stride on/after `practice_settings.keap_sync_start_at`, plus older patients only if `keap_include_existing_patients=true` (the client has not decided whether the first full load goes to Keap; default **false**). Linked to a lead → always in scope. |
| R3 | **Contact fields**: given/family name, email, phone, birth date (`YYYY-MM-DD`), contact type `keap_contact_type` (default `'Lead'`, confirm exact value in sandbox). Prefer Stride values when linked (the clinic's record is more accurate), else the lead's — except the phone, where the lead's number comes first because it is the one we call and text. Email: Stride `preferred_email` → `personal_email` → `work_email` → lead `email`. Phone: lead `phone_e164` → Stride `preferred_phone` → `mobile_phone` → `home_phone` → `work_phone`. Address only when there is no email and no phone (Keap needs at least one of email/phone/address). |
| R4 | **Pending tag** (`keap_pending_tag_id`): the person is a lead, the lead is not `booked`, and there is no counting appointment (R5). Removed once booked if `keap_remove_pending_when_booked=true` (default true). Walk-in Stride patients never get Pending. |
| R5 | **Counting appointment**: a `stride_appointments` row of the person's patient, not deleted, not cancelled, `start_utc >= keap_sync_start_at` (history before go-live never tags anyone), and its `appointment_type_id` is in `keap_appointment_types` (active). |
| R6 | **Booked tag of type T**: the person has a counting appointment of type T. If all of them are later cancelled/deleted, the Booked tag is removed when `keap_remove_booked_on_cancel=true` (default true — client to confirm). |
| R7 | **Completed tag of type T**: the person has a counting appointment of type T with status Checked In (`checked in` / `c`). Booked stays (do not remove Booked when Completed). No Show / Late Cancel: no tag. |
| R8 | **Date / time / clinician fields of type T**: from the **next upcoming** counting appointment of type T; if none upcoming, the **most recent past** counting one; if none, the field is cleared (`null`). Date = local date `YYYY-MM-DD`; time = local `h:mm AM/PM` (e.g. `2:30 PM`); clinician = `clinician_name`, else `stride_users` first + last name by `clinician_id`. Local = `practice_settings.stride_location_timezone`. |
| R9 | **Deletes in Stride never delete in Keap.** A deleted patient keeps its row; 2A stops changing it. A deleted lead (dashboard delete) → `lead_id` becomes null (FK `on delete set null`); keep the row and Keap contact. |
| R10 | **Test leads** (`is_test=true`) are skipped unless `KEAP_INCLUDE_TEST_LEADS=true` (true only while testing against the sandbox). |
| R11 | Values are what we **own**. 2B only touches the email slot EMAIL1, phone slot PHONE1, the tags in our mapping, and the custom fields in our mapping. Everything else in Keap belongs to the clinic. |

## 4. Database — migration `036_keap_sync.sql`

Write it re-runnable (`if not exists`), enable RLS on new tables (no policies = backend only), comments
explaining *why*, like `032_stride_import.sql`.

```sql
-- Practice-level switches for the Keap sync (all decided by the client / Keap admin).
alter table public.practice_settings
  add column if not exists keap_pending_tag_id text,
  add column if not exists keap_contact_type text not null default 'Lead',
  add column if not exists keap_sync_start_at timestamptz,          -- go-live cutoff (R2, R5); null = sync off
  add column if not exists keap_include_existing_patients boolean not null default false,
  add column if not exists keap_remove_pending_when_booked boolean not null default true,
  add column if not exists keap_remove_booked_on_cancel boolean not null default true;

-- One row per Stride appointment type we report to Keap. IDs come from the Keap
-- admin's table (tags + custom fields), per Keap account: sandbox and production differ.
create table if not exists public.keap_appointment_types (
  id bigint generated always as identity primary key,
  practice_id bigint not null references public.practices(id) on delete restrict,
  stride_appointment_type_id bigint not null,
  label text not null,                       -- e.g. 'Initial Evaluation - Ortho' (for humans)
  booked_tag_id text not null,
  completed_tag_id text,
  date_field_id text not null,
  time_field_id text,
  clinician_field_id text,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(practice_id, stride_appointment_type_id)
);

-- One row per person in Keap: the queue between 2A (desired) and 2B (synced).
create table if not exists public.keap_sync (
  id bigint generated always as identity primary key,
  practice_id bigint not null references public.practices(id) on delete restrict,
  lead_id uuid references public.leads(id) on delete set null,
  stride_patient_id bigint,
  keap_contact_id text,                      -- Keap ids are strings
  desired jsonb,                             -- section 5, written by 2A
  desired_hash text,
  desired_at timestamptz,
  synced jsonb,                              -- what 2B last made true in Keap
  synced_hash text,
  synced_at timestamptz,
  status text not null default 'pending' check(status in
    ('pending','syncing','synced','retrying','failed','skipped','needs_review')),
  attempts integer not null default 0,
  next_attempt_at timestamptz not null default now(),
  last_error text,                           -- PHI-free reason only
  skip_reason text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (lead_id is not null or stride_patient_id is not null or keap_contact_id is not null)
);
create unique index if not exists idx_keap_sync_lead on public.keap_sync(lead_id) where lead_id is not null;
create unique index if not exists idx_keap_sync_patient
  on public.keap_sync(practice_id, stride_patient_id) where stride_patient_id is not null;
create index if not exists idx_keap_sync_due
  on public.keap_sync(next_attempt_at, id) where status in ('pending','retrying');

alter table public.keap_appointment_types enable row level security;
alter table public.keap_sync enable row level security;
```

Apply locally, then to **demo** (`rpt migrate` with the demo `.env` on the server, see `../Deployment.md`:
build → `docker compose -f docker-compose.prod.yml run --rm api rpt migrate` → `up -d`). Production only
when the user says so.

## 5. The `desired` document (contract with 2B — do not change shape without updating 2B)

(Comments are for reading only; the stored JSON has none.)

```jsonc
{
  "v": 1,
  "contact": {
    "given_name": "Riya",
    "family_name": "Shah",
    "email": "riya@example.test",          // or null
    "phone": "+15615550123",               // E.164 or null
    "birth_date": "1990-02-03",            // or null
    "contact_type": "Lead",
    "address": null                        // or {"line1","line2","locality","region_code":"US-FL","postal_code","country_code":"USA"} — only per R3
  },
  "match": {                               // how 2B finds an existing Keap contact before creating
    "email": "riya@example.test",
    "phone_digits": "5615550123",          // national digits, for Keap phone_number filter
    "given_name": "Riya",
    "family_name": "Shah"
  },
  "tags": ["101", "205"],                  // tag ids that MUST be on the contact (sorted)
  "owned_tags": ["101", "205", "206", "301", "302"],  // every tag id we manage (pending + all mapped); 2B may remove only these
  "fields": {                              // custom field id -> semantic value (2B formats per Keap field type)
    "17": {"kind": "date", "value": "2026-10-15"},
    "18": {"kind": "time", "value": "2:30 PM"},
    "19": {"kind": "text", "value": "Jane Clinician"},
    "27": {"kind": "date", "value": null}  // null = clear it
  },
  "owned_fields": ["17", "18", "19", "27", "28", "29"],
  "sources": {"lead_id": "uuid-or-null", "stride_patient_id": 50044}
}
```

- `desired_hash` = `sha256(json.dumps(desired, sort_keys=True, separators=(",", ":")))`, hex.
- `tags` and lists are **sorted** so the hash is stable.
- `fields` contains **every** owned field (null when nothing to show), so a cleared date really clears.
- `sources` is informational; 2B does not need it.

## 6. Code — `src/rpt_agent/services/keap_desired.py`

Public functions (each runs inside the caller's `transaction()`):

```python
def attach_people(conn, practice_id: int) -> dict[str, int]:
    """Make sure every in-scope person has exactly one keap_sync row (R1, R2, R10)."""

def build_desired(conn, practice_id: int, row: dict, settings: dict, mapping: list[dict]) -> dict | None:
    """Return the desired document for one keap_sync row, or None to skip (with a reason)."""

def plan_changes(conn, practice_id: int, *, full: bool = False, limit: int = 500) -> dict[str, int]:
    """Recompute desired for changed people; queue rows whose hash changed. Returns counts."""
```

### 6.1 `attach_people`
1. Insert a row for each lead (`practice_id`, not `is_test` unless setting) with no row yet:
   `insert … select … where not exists (select 1 from keap_sync k where k.lead_id=l.id)`.
2. For each Stride patient linked to a lead (`stride_patients.lead_id`): if the lead's row has no
   `stride_patient_id`, set it. If a separate **patient-only** row exists for that patient:
   - same `keap_contact_id` or patient row has none → delete the patient row (merge into the lead row; keep
     the lead row's `keap_contact_id`, or take the patient row's if the lead row has none);
   - **different** non-null `keap_contact_id`s → set both rows `status='needs_review'`,
     `last_error='duplicate Keap contacts for one person'` (staff merge in Keap; never auto-merge).
3. Insert a patient-only row for each in-scope Stride patient (R2) with no row and no lead.
4. Return counts `{"leads_added", "patients_added", "merged", "duplicates"}`.

### 6.2 `build_desired`
Load the lead (if any), the patient (if any), the patient's counting appointments (R5, one query), the
`stride_users` names for clinician ids, the settings and the mapping. Apply R3–R8. Return `None` and set
`status='skipped', skip_reason=…` when:
- the person has no email, no phone and no address (`no contact details`);
- `keap_sync_start_at` is null (`keap sync not started`);
- the patient is deleted and the row was never synced (`deleted in Stride before sync`).
Never raise for one bad person: catch, mark that row `failed` with a PHI-free reason, continue.

### 6.3 `plan_changes` (what the worker calls every tick)
1. `attach_people`.
2. Pick rows to recompute (`full=False`): rows whose sources changed since `desired_at` —
   `leads.updated_at`, `stride_patients.updated_at`, any of the patient's `stride_appointments.updated_at`,
   or `desired_at is null`; plus rows with any appointment whose `start_utc` passed since `desired_at`
   (the "next upcoming" date moves with time). With `full=True` (hourly) recompute all non-skipped rows.
   Batch with `limit`, oldest `desired_at` first.
3. For each: `desired = build_desired(...)`; if `sha256 != desired_hash` →
   `update keap_sync set desired=…, desired_hash=…, desired_at=now(), status = case when status in
   ('syncing','needs_review') then status else 'pending' end, attempts=0, next_attempt_at=now()`
   (`needs_review` waits for a person; `skipped` rows whose sources changed are recomputed and leave
   `skipped` once they have contact details). If the hash is
   unchanged just bump `desired_at`.
   (2B re-checks the hash when it finishes a sync, so a change during `syncing` is not lost.)
4. Return counts `{"recomputed", "queued", "skipped", "failed"}`.

Performance: a few thousand people is fine per tick; never load all appointments into Python at once —
query per batch with `= any(%s)` on patient ids.

### 6.4 Priority (used by 2B when ordering)
Leave `next_attempt_at=now()` for leads; for patient-only rows created by a bulk first load
(more than 200 queued in one tick) set `next_attempt_at = now() + interval '1 minute' * (n / 200)` so new
leads are never stuck behind a backlog.

## 7. CLI — `rpt keap-plan`

Add to `cli.py`: `rpt keap-plan [--full] [--dry-run] [--limit N]`. Prints counts and, for up to 5 rows, a
**masked** desired document (names → first letter + `***`, email/phone masked, keep tag/field ids). With
`--dry-run` it computes but does not write (use a transaction that is rolled back). This is how a human
checks 2A before 2B exists.

## 8. Configuration

`config.py` + `.env.example`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `KEAP_INCLUDE_TEST_LEADS` | `false` | R10; set `true` only while testing against the Keap sandbox |
| `KEAP_PLAN_BATCH` | `500` | rows recomputed per tick |

Everything else is per practice in `practice_settings` / `keap_appointment_types` (filled with SQL; IDs
come from Phase 2B's `rpt keap-discover`).

## 9. Testing

### 9.1 Local database (never the live `.env` DB)
No Docker needed. An embedded Postgres 16 runs from the `pgserver` pip package; see `tests/CLAUDE.md`
(register an empty `pgcrypto` extension once, migration 001 creates it but `gen_random_uuid` is built in).

```bash
~/.local/bin/uv run --no-project --with pgserver --with 'psycopg[binary]' python -c \
 "import pgserver; s=pgserver.get_server('/tmp/rptpg', cleanup_mode=None); print(s.get_uri())"
# create database rpt_local + roles anon/authenticated/service_role, then:
export SUPABASE_DB_URL=<local uri for rpt_local> TEST_DATABASE_URL=$SUPABASE_DB_URL APP_ENV=development
~/.local/bin/uv run --frozen rpt migrate && ~/.local/bin/uv run --frozen rpt seed
```

### 9.2 Tests to write
`tests/test_keap_desired.py` (pure functions, no DB) and `tests/test_keap_desired_integration.py`
(`TEST_DATABASE_URL`, same fixture style as `tests/test_stride_import_integration.py`: create a practice,
settings, mapping, synthetic leads with `555` phones and `example.test` emails, import Stride CSV rows with
the helpers there, then call `plan_changes`). Each test asserts on `keap_sync.desired`.

| Case | Expected desired |
| --- | --- |
| Sheet lead, no appointment | contact from lead, `tags=[pending]` |
| Lead with only `full_name` "Asha Rao" | given `Asha`, family `Rao` |
| Lead books (counting Initial Evaluation tomorrow) | `tags=[IE booked]`, Pending gone, IE date/time/clinician set |
| `keap_remove_pending_when_booked=false` | Pending + IE booked |
| Lead `status='booked'` by voice agent, no Stride appointment yet | Pending gone, no Booked yet |
| Walk-in Stride patient with Follow-up | patient-only row, `tags=[FU booked]`, no Pending |
| Appointment Checked In | Booked + Completed of that type |
| Only appointment cancelled | Booked removed (setting true) / kept (false); date field `null` |
| Rescheduled (same id, new date) | date/time follow it |
| Two upcoming of same type | nearest upcoming wins |
| Only past counting appointment | its date shown |
| Appointment before `keap_sync_start_at` | ignored entirely |
| Appointment type not mapped | ignored |
| Stride email differs from lead email | Stride wins (R3) |
| No email/phone, has address | address included |
| No email/phone/address | row `skipped`, reason `no contact details` |
| Lead row synced (contact 8842) then patient linked | patient attached to lead row, no new row |
| Patient row synced (9001) and lead row synced (8842), then linked | both `needs_review` |
| Patient deleted in Stride | row unchanged, hash unchanged |
| `is_test` lead | no row (unless setting) |
| Recompute with no change | hash equal, status not reset |
| Change while `syncing` | status stays `syncing`, hash updated |
| Daylight saving: appointment 2:30 PM local in March and November | time `2:30 PM` both |
| Clinician name missing on appointment | taken from `stride_users` |
| Desired JSON contains no PHI in `last_error`/logs | assert on captured logs |

Also: `rpt keap-plan --dry-run` writes nothing.

### 9.3 Mock data end to end (local)
1. Seed mapping (fake ids are fine for 2A): two types, e.g. Stride type 1452 "Initial Evaluation" →
   booked `101`, completed `102`, date `17`, time `18`, clinician `19`; 1451 "Follow-up" → `201`, `202`,
   `27`, `28`, `29`; pending tag `100`; `keap_sync_start_at = now() - interval '1 day'`.
2. `scripts/fake_stride.py seed-leads` then `server` + `feed` (see `docs/PHASE2.md` §12) with the Stride
   import worker running locally against the **local** DB. Use appointment type ids your mapping covers
   (edit `TYPES` in the script if needed).
3. `rpt keap-plan` after each round and check the masked output: new walk-ins get Booked, the seeded Sheet
   leads go Pending → Booked, cancels remove Booked, check-ins add Completed.

## 10. Done when
- Migration 036 applied locally and on demo; full test suite + `ruff check src tests` green.
- `rpt keap-plan` on demo shows sensible masked output for real demo leads.
- `docs/PHASE2.md` and `context/PROJECT_CONTEXT.md` changelog updated; folder `CLAUDE.md` tables list
  the new module/tables.

## 11. Open questions (code must follow the defaults above until answered)
1. Exact Keap `contact_type` value for "Lead" (check one sandbox contact via 2B's tooling).
2. Remove Booked on cancel? (default yes) — client.
3. Send existing Stride patients from the first full load? (default no) — client.
4. Two patients sharing one email → two contacts (current behaviour). Confirm with client.
5. Pending for leads that end Do Not Contact / Closed — keep Pending (current) or remove?
