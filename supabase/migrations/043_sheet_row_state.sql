-- Direct Google Sheet sync (replaces the n8n Sheet workflows, 2026-10-14).
-- One row per linked Sheet lead: a fingerprint of the profile cells staff edit (name, phone,
-- email, date of birth, location, case). The sheet-worker reads the whole tab every poll and
-- sends a profile update only when this fingerprint changes, so its own write-backs (status,
-- outcomes, links) never trigger anything. Commands (Action + Outcome Status) need no state:
-- a blank Outcome Status is the "to do" marker, as it was in n8n.
create table if not exists public.sheet_row_state (
  lead_id uuid primary key references public.leads(id) on delete cascade,
  practice_id bigint not null references public.practices(id) on delete cascade,
  profile_hash text not null,
  row_number integer,
  seen_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.sheet_row_state enable row level security;
