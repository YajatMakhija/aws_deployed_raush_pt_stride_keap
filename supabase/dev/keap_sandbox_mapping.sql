-- Keap SANDBOX (puq284) mapping for the mock Stride appointment types (scripts/fake_stride.py TYPES).
-- Tag and field ids come from `rpt keap-discover` against the sandbox; the client's
-- production Keap has different ids, so never reuse this file there.
-- Tag pairing from the Keap admin's list (2026-10-20); the Stride type ids are mocks until
-- Rausch's real appointment types are known. Needs migration 046 (male_booked_tag_id).
-- Initial Evaluation, Re-Evaluation and Plan of Care (POC) are deliberately NOT mapped for now
-- (client, 2026-10-20): they get no Keap tag, and any earlier rows for them are switched off below.
update public.practice_settings set
  keap_pending_tag_id = '144',          -- Pending
  keap_gave_up_tag_id = '186',          -- STATUS: AI Gave Up
  keap_sync_start_at = coalesce(keap_sync_start_at, now() - interval '1 day'),
  keap_include_existing_patients = true -- sandbox: we want to test the full first load
where practice_id = (select id from public.practices where slug = 'rausch-pt');

insert into public.keap_appointment_types
  (practice_id, stride_appointment_type_id, label, booked_tag_id, male_booked_tag_id,
   completed_tag_id, date_field_id, time_field_id, clinician_field_id)
select p.id, v.type_id, v.label, v.booked, v.male, v.done, '6', '8', '10'
from public.practices p,
  (values (1, 'PT',                 '184', null,  '176'),  -- PT Appt Booked / PT Appt Completed
          (5, 'PT Premium',         '166', null,  '164'),  -- PT Premium Appt Booked / Completed
          (6, 'Pelvic',             '162', '160', '158'))  -- Pelvic Booked Female / Male; Pelvic Completed
       as v(type_id, label, booked, male, done)
where p.slug = 'rausch-pt'
on conflict (practice_id, stride_appointment_type_id) do update set
  label = excluded.label, booked_tag_id = excluded.booked_tag_id,
  male_booked_tag_id = excluded.male_booked_tag_id,
  completed_tag_id = excluded.completed_tag_id, date_field_id = excluded.date_field_id,
  time_field_id = excluded.time_field_id, clinician_field_id = excluded.clinician_field_id,
  is_active = true;

-- New Evaluation Booked (180), Re-Eval Booked (178), POC Appt Booked (174): off, whatever Stride
-- type id they had.
update public.keap_appointment_types set is_active = false, updated_at = now()
where booked_tag_id in ('180', '178', '174')
  and practice_id = (select id from public.practices where slug = 'rausch-pt');
