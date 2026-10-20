-- Pelvic bookings have one Keap tag per sex ("STATUS: Pelvic Appt Booked - Female" / "- Male",
-- Keap admin 2026-10-20). booked_tag_id stays the tag for everyone else; a male patient (Stride
-- gender M / Male) gets male_booked_tag_id instead when it is set. Null = one tag for all.
alter table public.keap_appointment_types
  add column if not exists male_booked_tag_id text;
