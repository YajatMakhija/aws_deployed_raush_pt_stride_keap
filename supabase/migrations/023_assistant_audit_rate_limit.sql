-- Keep production assistant access auditing and per-actor rate checks indexed.
create index if not exists idx_dashboard_audit_actor_action_created
  on public.dashboard_audit_log(actor_id,action,created_at desc);
