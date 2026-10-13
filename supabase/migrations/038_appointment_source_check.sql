-- appointments.booking_source has carried this check constraint on the live databases since early
-- setup, but no migration file declared it, so local test databases accepted values the live ones
-- refuse (a new 'staff' source broke dashboard bookings on demo, 2026-10-14). Declare it here;
-- a database that already has it is left unchanged.
do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conrelid = 'public.appointments'::regclass and conname = 'appointments_booking_source_check'
  ) then
    alter table public.appointments add constraint appointments_booking_source_check
      check (booking_source in ('link', 'call', 'sms', 'manual', 'voice_agent'));
  end if;
end $$;
