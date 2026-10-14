-- Other names staff and patients use for a case type ("Ortho" in the Sheet's Case column, "pelvic
-- floor" on a call). The booking engine matched the Sheet's Case only by the exact name, so "Pelvic"
-- fell back to the default case and a pelvic patient was offered general Physical Therapy.
alter table public.case_types add column if not exists aliases text[] not null default '{}';

update public.case_types set aliases = array['ortho','orthopedic','orthopaedic','ortho pt','pt','general pt']
 where lower(name) = 'physical therapy' and aliases = '{}';
update public.case_types set aliases = array['pelvic','pelvic floor','pelvic health','pelvic pt','pelvic floor pt','womens health','women''s health']
 where lower(name) like '%pelvic%' and aliases = '{}';
