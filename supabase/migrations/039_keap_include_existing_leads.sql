-- Leads created before keap_sync_start_at go to Keap only when this is true (like
-- keap_include_existing_patients). Off by default: pushing every old lead at once can fire Keap
-- automations on the Pending / AI Gave Up tags for people who left long ago. The client decides
-- (2026-10-14: demo sends new leads only).
alter table public.practice_settings
  add column if not exists keap_include_existing_leads boolean not null default false;
