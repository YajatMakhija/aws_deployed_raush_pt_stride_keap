-- Prevent stale worker deployments from claiming new outreach and guarantee that
-- every terminal dispatch failure reaches the n8n Sheet outbox.

create or replace function public.guard_outreach_worker_claim()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if old.status = 'planned'
     and new.status = 'in_flight'
     and coalesce(current_setting('rpt.worker_claim_protocol', true), '') <> 'v1' then
    return null;
  end if;
  return new;
end;
$$;

drop trigger if exists trg_guard_outreach_worker_claim on public.outreach_events;
create trigger trg_guard_outreach_worker_claim
before update of status on public.outreach_events
for each row execute function public.guard_outreach_worker_claim();

create or replace function public.enqueue_failed_outreach_sheet_sync()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  sync_type text;
begin
  if old.status is distinct from new.status and new.status in ('failed', 'unknown') then
    sync_type := case when new.status = 'failed' then 'outreach_failed' else 'outreach_unknown' end;
    insert into public.integration_outbox(
      event_id, event_type, aggregate_id, payload, status, destination, outreach_event_id
    ) values (
      'n8n:db:' || sync_type || ':' || new.id::text,
      'sheet.' || sync_type,
      new.lead_id::text,
      jsonb_build_object(
        'lead_id', new.lead_id::text,
        'reason', sync_type,
        'outreach_event_id', new.id
      ),
      'pending',
      'n8n',
      new.id
    ) on conflict(event_id) do nothing;
  end if;
  return new;
end;
$$;

drop trigger if exists trg_failed_outreach_sheet_sync on public.outreach_events;
create trigger trg_failed_outreach_sheet_sync
after update of status on public.outreach_events
for each row execute function public.enqueue_failed_outreach_sheet_sync();
