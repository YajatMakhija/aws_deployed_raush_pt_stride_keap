-- Sheet synchronization belongs only to leads created through Google Sheets.
-- This replaces the migration-025 function without changing its trigger.

create or replace function public.enqueue_failed_outreach_sheet_sync()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  sync_type text;
begin
  if old.status is distinct from new.status
     and new.status in ('failed', 'unknown')
     and exists (
       select 1 from public.leads
       where id = new.lead_id and source_system = 'google_sheets'
     ) then
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
