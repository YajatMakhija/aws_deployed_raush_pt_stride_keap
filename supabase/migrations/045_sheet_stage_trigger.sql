-- The Sheet's Lead Status mirrors the dashboard column (client decision 2026-10-15). Many code paths
-- change a lead's status, cadence_state or needs_review (dashboard pause/resume, board moves, review
-- resolve, call outcomes, the worker's review pause, Stride import). Rather than trust each one to
-- queue a Sheet update, any such change on a Google Sheet lead queues one here. The sheet-worker groups
-- a lead's pending updates into one snapshot, so a path that also queues its own costs nothing extra.
create or replace function public.enqueue_sheet_stage_change() returns trigger
language plpgsql as $$
begin
  if new.source_system = 'google_sheets' and (
       new.status is distinct from old.status
       or new.cadence_state is distinct from old.cadence_state
       or new.needs_review is distinct from old.needs_review) then
    insert into public.integration_outbox(event_id,event_type,aggregate_id,payload,status,destination)
    values ('n8n:stage_changed:' || md5(new.id::text || txid_current()::text || clock_timestamp()::text),
            'sheet.stage_changed', new.id::text,
            jsonb_build_object('lead_id', new.id::text, 'reason', 'stage_changed'), 'pending', 'n8n')
    on conflict (event_id) do nothing;
  end if;
  return new;
end
$$;

drop trigger if exists leads_sheet_stage_change on public.leads;
create trigger leads_sheet_stage_change
  after update of status, cadence_state, needs_review on public.leads
  for each row execute function public.enqueue_sheet_stage_change();
