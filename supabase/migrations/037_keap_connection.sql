-- Keap sync, phase 2B: OAuth tokens for the Keap account this practice syncs to.
-- Backend only: never exposed to the dashboard, n8n or logs. Tokens rotate on every
-- refresh (the old refresh token stops working), so both are saved together.
create table if not exists public.keap_connections (
  practice_id bigint primary key references public.practices(id) on delete restrict,
  keap_account text,
  access_token text not null,
  refresh_token text not null,
  access_token_expires_at timestamptz not null,
  status text not null default 'active' check(status in ('active','reauthorization_required')),
  last_refreshed_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
drop trigger if exists trg_keap_connections_updated on public.keap_connections;
create trigger trg_keap_connections_updated before update on public.keap_connections
  for each row execute function public.set_updated_at();
alter table public.keap_connections enable row level security;
