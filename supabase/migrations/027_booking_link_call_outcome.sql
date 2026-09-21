-- A call that ends with the patient asking for the booking link had no outcome
-- of its own, so it was stored as 'manual' and read on the dashboard as
-- "answered, no outcome recorded".

alter table public.outreach_events
  drop constraint if exists outreach_events_outcome_check,
  add constraint outreach_events_outcome_check
    check (outcome is null or outcome in ('booked','not_interested','no_answer','voicemail',
      'callback','transferred','manual','call_opt_out','do_not_contact','booking_link'));
