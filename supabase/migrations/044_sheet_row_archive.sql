-- Nightly Sheet archive (2026-10-15): finished rows move from the working tab to an "Archive" tab
-- so the tab staff work in (and every poll) stays small. archived_at marks the lead's row as moved;
-- a later Sheet update for that lead is then dropped quietly instead of retrying "not found".
alter table public.sheet_row_state add column if not exists archived_at timestamptz;
