-- Allow free-text Sheet Title / API lead_type values on leads.
-- Backend validation keeps the value non-empty and at most 200 characters.
-- title and lead_type are API aliases for the same stored leads.lead_type text.

alter table public.leads
  drop constraint if exists leads_lead_type_check;
