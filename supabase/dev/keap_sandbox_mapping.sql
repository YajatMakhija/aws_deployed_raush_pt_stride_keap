-- Keap SANDBOX (puq284) mapping for the three mock Stride appointment types.
-- Tag and field ids come from `rpt keap-discover` against the sandbox; the client's
-- production Keap has different ids, so never reuse this file there.
-- Draft pairing of Stride types to Michelle's STATUS tags, to be confirmed by her.
update public.practice_settings set
  keap_pending_tag_id = '144',          -- Pending
  keap_gave_up_tag_id = '186',          -- STATUS: AI Gave Up
  keap_sync_start_at = coalesce(keap_sync_start_at, now() - interval '1 day'),
  keap_include_existing_patients = true -- sandbox: we want to test the full first load
where practice_id = (select id from public.practices where slug = 'rausch-pt');

insert into public.keap_appointment_types
  (practice_id, stride_appointment_type_id, label, booked_tag_id, completed_tag_id,
   date_field_id, time_field_id, clinician_field_id)
select p.id, v.type_id, v.label, v.booked, v.done, '6', '8', '10'
from public.practices p,
  (values (2, 'Initial Evaluation', '180', '172'),   -- New Evaluation Booked / Eval Completed
          (1, 'Follow-up',          '184', '176'),   -- PT Appt Booked / PT Appt Completed
          (3, 'Progress Note',      '174', '156'))   -- POC Appt Booked / PT Completed their POC
       as v(type_id, label, booked, done)
where p.slug = 'rausch-pt'
on conflict (practice_id, stride_appointment_type_id) do update set
  label = excluded.label, booked_tag_id = excluded.booked_tag_id,
  completed_tag_id = excluded.completed_tag_id, date_field_id = excluded.date_field_id,
  time_field_id = excluded.time_field_id, clinician_field_id = excluded.clinician_field_id,
  is_active = true;
