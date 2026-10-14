-- The dashboard calendar, home page and analytics read bookings by practice and start time,
-- ours and Stride's. Without these, each view scans every appointment the clinic ever had
-- (a full Stride history is tens of thousands of rows). Read-only views; no data changes.
create index if not exists idx_stride_appointments_practice_start
  on public.stride_appointments (practice_id, start_utc) where deleted_at is null;
create index if not exists idx_appointments_practice_start
  on public.appointments (practice_id, start_utc);
-- Booking -> the call it was made on (Elliot's transcript link on the booking card).
create index if not exists idx_appointments_call_id
  on public.appointments (call_id) where call_id is not null;
