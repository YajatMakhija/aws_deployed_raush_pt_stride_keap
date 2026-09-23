-- One rule, one owner. The application decides what a review means: the lead's
-- status and the reason staff read. This trigger is only the fail-safe for a
-- path that never reaches that code - a stale worker, a direct SQL fix - so it
-- does the mechanical part and nothing else: flag the lead, stop the schedule.
--
-- It used to also set status='needs_attention' and overwrite review_reason,
-- which made it the last writer on every failure and silently overrode the
-- application. Keep them in step: this function must not set leads.status.

create or replace function public.pause_failed_outreach_for_review()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if old.status is distinct from new.status and new.status in ('failed', 'unknown') then
    update public.leads
    set needs_review = true,
        review_reason = coalesce(review_reason, case
          when new.channel = 'sms' then 'cadence SMS was not delivered'
          else coalesce(new.failure_reason, 'outreach delivery requires review')
        end),
        review_flagged_at = coalesce(review_flagged_at, now()),
        cadence_state = 'paused'
    where id = new.lead_id
      and status not in ('booked', 'declined', 'do_not_contact', 'closed_no_response');

    update public.outreach_events
    set status = 'skipped',
        failure_reason = 'paused_for_review',
        updated_at = now()
    where lead_id = new.lead_id
      and status = 'planned';
  end if;
  return new;
end;
$$;
