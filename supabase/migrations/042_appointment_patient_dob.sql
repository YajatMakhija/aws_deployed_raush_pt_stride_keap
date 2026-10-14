-- The date of birth the patient confirmed on the booking call. When it differs from the lead's
-- (the Sheet's), the booking still goes ahead with the patient's date (Stride needs the right one)
-- and staff are told on the dashboard and in the Sheet to correct the Sheet (client choice,
-- 2026-10-14). Null for bookings made before this column, or by staff.
alter table public.appointments add column if not exists patient_dob date;
