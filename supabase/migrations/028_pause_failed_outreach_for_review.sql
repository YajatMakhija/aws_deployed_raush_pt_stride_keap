-- A terminal provider failure requires staff review and must stop later cadence
-- steps from being claimed. The application performs the same update; this
-- trigger is the database fail-safe for every outreach settlement path.

create or replace function public.pause_failed_outreach_for_review()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if old.status is distinct from new.status and new.status in ('failed', 'unknown') then
    update public.leads
    set needs_review = true,
        review_reason = case
          when new.channel = 'sms' then 'cadence SMS was not delivered'
          else coalesce(new.failure_reason, 'outreach delivery requires review')
        end,
        review_flagged_at = now(),
        status = case
          when status = 'invalid_phone' then status
          else 'needs_attention'
        end,
        cadence_state = 'paused',
        status_changed_at = now()
    where id = new.lead_id
      and status not in ('booked', 'declined', 'do_not_contact', 'closed_no_response');
  end if;
  return new;
end;
$$;

drop trigger if exists trg_pause_failed_outreach_for_review on public.outreach_events;
create trigger trg_pause_failed_outreach_for_review
after update of status on public.outreach_events
for each row execute function public.pause_failed_outreach_for_review();
