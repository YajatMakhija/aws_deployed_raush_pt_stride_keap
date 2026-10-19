-- Calling hours and holidays confirmed by the clinic (Emma, 2026-10-19).
-- Mon-Fri 8:00 AM-7:00 PM, Sat 8:00 AM-1:30 PM, Sun closed (California time). New contact may start until
-- 15 minutes before closing (6:45 PM / 1:15 PM; worker.LAST_START_BEFORE_CLOSE). Daily limits unchanged.
-- No outreach on: Labor Day, Memorial Day, 4th of July, Christmas Eve, Christmas, New Year's Eve,
-- New Year's Day, Thanksgiving. Dates listed to the end of 2028: add the next year's before then.
-- Run on demo first, then production with the owner's go. Safe to run again.
update public.practice_settings ps
set business_hours = '{
      "1": {"open": "08:00", "close": "19:00"},
      "2": {"open": "08:00", "close": "19:00"},
      "3": {"open": "08:00", "close": "19:00"},
      "4": {"open": "08:00", "close": "19:00"},
      "5": {"open": "08:00", "close": "19:00"},
      "6": {"open": "08:00", "close": "13:30"},
      "7": null
    }'::jsonb,
    holidays = '[
      "2026-11-26", "2026-12-24", "2026-12-25", "2026-12-31",
      "2027-01-01", "2027-05-31", "2027-07-04", "2027-09-06", "2027-11-25", "2027-12-24", "2027-12-25", "2027-12-31",
      "2028-01-01", "2028-05-29", "2028-07-04", "2028-09-04", "2028-11-23", "2028-12-24", "2028-12-25", "2028-12-31"
    ]'::jsonb,
    updated_at = now()
from public.practices p
where p.id = ps.practice_id and p.slug = 'rausch-pt'
returning p.slug, ps.business_hours, jsonb_array_length(ps.holidays) as holidays;
