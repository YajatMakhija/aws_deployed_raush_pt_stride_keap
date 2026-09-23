-- Allow the Sheet team to mark a lead booked, and make the database failure
-- fail-safe stop every remaining planned cadence step.

alter table public.lead_action_requests
  drop constraint if exists lead_action_requests_action_check;

alter table public.lead_action_requests
  add constraint lead_action_requests_action_check
  check (action in ('start_cadence', 'restart_cadence', 'do_not_contact', 'booked'));

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
