-- Keap sync, phase 2A: decide what each person's Keap contact should look like.
-- Nothing here talks to Keap. keap_desired.py writes keap_sync.desired; the
-- keap-worker (037) reads it and writes keap_sync.synced.
--
-- Everything is re-runnable (if not exists) and backend-only: RLS is on and there
-- are no policies, so only the service role can read these tables.

-- Practice-level switches, all decided by the client / the Keap admin.
alter table public.practice_settings
  add column if not exists keap_pending_tag_id text,
  -- Tag for a lead whose follow-up ended without a booking; it replaces Pending
  -- (applied first, so a person is never left with no status tag).
  add column if not exists keap_gave_up_tag_id text,
  add column if not exists keap_contact_type text not null default 'Lead',
  -- Go-live cutoff: appointments before it never tag anyone, and a null value
  -- keeps the whole sync switched off.
  add column if not exists keap_sync_start_at timestamptz,
  add column if not exists keap_include_existing_patients boolean not null default false,
  add column if not exists keap_remove_pending_when_booked boolean not null default true,
  add column if not exists keap_remove_booked_on_cancel boolean not null default true;

-- One row per Stride appointment type we report to Keap. Tag ids come from the Keap
-- admin's account (sandbox and production have different ids). The date/time/clinician
-- custom fields are one shared set in Keap, so those columns repeat on every row.
create table if not exists public.keap_appointment_types (
  id bigint generated always as identity primary key,
  practice_id bigint not null references public.practices(id) on delete restrict,
  stride_appointment_type_id bigint not null,
  label text not null,
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

-- One row per person in Keap: the queue between phase 2A (desired) and 2B (synced).
create table if not exists public.keap_sync (
  id bigint generated always as identity primary key,
  practice_id bigint not null references public.practices(id) on delete restrict,
  -- A lead that is deleted on the dashboard keeps its row (and its Keap contact).
  lead_id uuid references public.leads(id) on delete set null,
  stride_patient_id bigint,
  keap_contact_id text,
  desired jsonb,
  desired_hash text,
  desired_at timestamptz,
  synced jsonb,
  synced_hash text,
  synced_at timestamptz,
  status text not null default 'pending' check(status in
    ('pending','syncing','synced','retrying','failed','skipped','needs_review')),
  attempts integer not null default 0,
  next_attempt_at timestamptz not null default now(),
  last_error text,
  skip_reason text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create unique index if not exists idx_keap_sync_lead
  on public.keap_sync(lead_id) where lead_id is not null;
create unique index if not exists idx_keap_sync_patient
  on public.keap_sync(practice_id, stride_patient_id) where stride_patient_id is not null;
-- One Keap contact belongs to one person. Two rows pointing at the same contact would
-- fight over its tags (one says Pending, the other Booked) every time either changed.
create unique index if not exists idx_keap_sync_contact
  on public.keap_sync(practice_id, keap_contact_id) where keap_contact_id is not null;
create index if not exists idx_keap_sync_due
  on public.keap_sync(next_attempt_at, id) where status in ('pending','retrying');
create index if not exists idx_keap_sync_status on public.keap_sync(practice_id, status);

drop trigger if exists trg_keap_sync_updated on public.keap_sync;
create trigger trg_keap_sync_updated before update on public.keap_sync
  for each row execute function public.set_updated_at();
drop trigger if exists trg_keap_types_updated on public.keap_appointment_types;
create trigger trg_keap_types_updated before update on public.keap_appointment_types
  for each row execute function public.set_updated_at();

alter table public.keap_appointment_types enable row level security;
alter table public.keap_sync enable row level security;
