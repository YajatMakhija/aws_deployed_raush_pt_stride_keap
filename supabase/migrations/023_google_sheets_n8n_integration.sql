-- Durable Google Sheets/n8n intake and status synchronization.
-- This migration is additive and safe to deploy before enabling either workflow.

-- Existing outbox rows are Keap handoffs. Destination routing prevents the
-- Keap worker and the Sheet worker from claiming each other's work.
alter table public.integration_outbox
  add column if not exists destination text;

update public.integration_outbox
set destination = 'keap'
where destination is null;

alter table public.integration_outbox
  alter column destination set default 'keap',
  alter column destination set not null,
  add column if not exists outreach_event_id bigint
    references public.outreach_events(id) on delete set null;

alter table public.integration_outbox
  drop constraint if exists integration_outbox_destination_check,
  add constraint integration_outbox_destination_check
    check (destination in ('keap', 'n8n'));

create index if not exists idx_outbox_destination_pending
  on public.integration_outbox(destination, next_attempt_at, id)
  where status = 'pending';

create index if not exists idx_outbox_outreach_event
  on public.integration_outbox(outreach_event_id)
  where outreach_event_id is not null;

-- The supplied/live schema already has this table, while older clean-schema
-- migrations do not. Creating it conditionally keeps both database histories
-- compatible and makes it the canonical transcript artifact store.
create table if not exists public.call_transcripts (
  id bigint generated always as identity primary key,
  call_log_id bigint not null unique
    references public.call_logs(id) on delete cascade,
  lead_id uuid not null references public.leads(id) on delete cascade,
  transcript_text text not null,
  summary text,
  search_vector tsvector generated always as
    (to_tsvector('english', transcript_text)) stored,
  created_at timestamptz not null default now()
);

alter table public.call_transcripts
  add column if not exists transcript_link text;

create index if not exists idx_call_transcripts_lead_created
  on public.call_transcripts(lead_id, created_at desc);

alter table public.call_transcripts enable row level security;

-- One row represents one client command, not one lead. Keeping the exact
-- response makes a retry after a lost HTTP response safe for Start, Restart,
-- and Do Not Contact.
create table if not exists public.lead_action_requests (
  request_id uuid primary key,
  practice_id bigint not null references public.practices(id) on delete restrict,
  action text not null check (action in ('start_cadence', 'restart_cadence', 'do_not_contact')),
  request_hash text not null check (char_length(request_hash) = 64),
  lead_id uuid references public.leads(id) on delete set null,
  status text not null default 'processing'
    check (status in ('processing', 'completed', 'failed')),
  http_status integer,
  response_body jsonb,
  error_category text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz
);

create index if not exists idx_lead_action_requests_lead
  on public.lead_action_requests(lead_id, created_at desc)
  where lead_id is not null;

create index if not exists idx_lead_action_requests_processing
  on public.lead_action_requests(created_at)
  where status = 'processing';

create index if not exists idx_leads_practice_phone
  on public.leads(practice_id, phone_e164)
  where phone_e164 is not null;

alter table public.lead_action_requests enable row level security;

drop trigger if exists trg_lead_action_requests_updated on public.lead_action_requests;
create trigger trg_lead_action_requests_updated
  before update on public.lead_action_requests
  for each row execute function public.set_updated_at();

comment on column public.integration_outbox.destination is
  'Delivery owner. Keap rows are processed by the outreach worker; n8n rows by the Sheet worker.';
comment on table public.lead_action_requests is
  'Server-only idempotency and recovery ledger for signed n8n lead actions.';
comment on column public.call_transcripts.transcript_link is
  'Authenticated dashboard URL for this transcript; never a provider recording URL.';
