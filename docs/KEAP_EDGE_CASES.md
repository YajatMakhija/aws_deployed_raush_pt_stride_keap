# Keap sync - edge cases, and where each is tested

The one rule the feature exists for: **Keap and the dashboard never disagree** about a person
(Keap must not say "Booked" while the dashboard says "needs attention"). Every chain test asserts
it through `agree()`.

Where tests live:

| Short name | File | What it drives |
| --- | --- | --- |
| **unit** | `tests/test_keap_desired.py` | the planning rules, no database |
| **plan** | `tests/test_keap_desired_integration.py` | planning against Postgres |
| **sync** | `tests/test_keap_sync_integration.py` | one person -> Keap (fake Keap) |
| **chain** | `tests/test_keap_chain_integration.py` | Sheet -> dashboard -> Stride CSV -> Keap. Runs on the fake Keap; with `KEAP_LIVE_SANDBOX=1` the same tests also run against the real sandbox |
| **e2e** | `scripts/keap_sandbox_e2e.py` | the live sandbox, a scripted set of personas |

Status: **tested**, **by design** (a deliberate rule, pinned by a test), **DECISION** (needs the
client / Michelle), **GAP** (known weakness, not built), **RISK** (cannot be tested here).

## A. A person's journey

| ID | Situation | What happens | Status / tests |
| --- | --- | --- | --- |
| E-01 | New Sheet lead | Contact created (name, email, phone, birthday, type Lead), tag Pending, appointment fields empty | tested - chain `test_sheet_lead_appears...`, unit, e2e |
| E-02 | Lead is still being called | Nothing changes; an idle system makes zero Keap writes | tested - chain `test_an_idle_system...` |
| E-03 | Lead books in Stride | Type's Booked tag applied **before** Pending is removed; date, time, clinician filled; dashboard says booked | tested - chain `test_stride_booking...`, sync (order of calls) |
| E-04 | Booking rescheduled | Date/time/clinician follow; tags unchanged | tested - chain `test_reschedule...` |
| E-05 | Booking cancelled / late-cancelled / deleted in Stride | Dashboard -> needs attention; Keap: Booked removed, Pending back, fields cleared | tested - chain (3 variants) |
| E-06 | Re-books after a cancel (even a different type) | Booked again, Pending off | tested - chain `test_rebooking...` |
| E-07 | Checked in | Completed added, Booked kept | tested - chain, plan |
| E-08 | Second appointment of another type | Both Booked tags; the single date/time/clinician set shows the **next upcoming** one (else the latest past) | tested - chain, unit |
| E-09 | Stride type we do not map (e.g. id 77) | Dashboard says booked, but Keap gets **no** Booked tag and Pending is removed -> the contact has no status tag | **by design / GAP** - pinned by chain `test_an_appointment_type_we_do_not_map...`. Fix = map every type Rausch uses (waiting for the real type list) |
| E-10 | Booked by voice agent or by staff in the Sheet, no Stride visit yet | Pending removed, no Booked tag | by design - chain `test_staff_marking_booked...`, unit |
| E-11 | Visit before the go-live cutoff (`keap_sync_start_at`) | Ignored; never tags anyone | tested - chain, unit, plan |
| E-12 | **No-show** | Dashboard flags the lead needs attention (the booking "fell through"); Keap **keeps Booked** (the rule we agreed: no-show = no tag change) -> **they disagree** | **DECISION** - pinned by chain `test_no_show_keeps_booked...`. Options: (a) keep, (b) remove Booked on no-show like a cancel, (c) add a No-show tag. Recommend (b) |
| E-13 | Follow-up ends with no booking (closed_no_response, declined) | AI Gave Up (186) applied first, then Pending removed | tested - chain, unit |
| E-14 | Do Not Contact / invalid phone / transferred to a human | Left on Pending, **no tag change** until Michelle names the tags. NB: a DNC tag alone does not stop email; Keap's email-opt-out call is a separate, unbuilt step | **DECISION** - chain `test_do_not_contact_is_left_alone...` |
| E-15 | Lead closed by staff | Same as E-14 unless status is closed_no_response/declined | DECISION |
| E-16 | Walk-in patient who was never a lead | Contact + Booked/Completed tags, never Pending | tested - chain, plan |
| E-17 | Existing patients on the first full load | Included when `keap_include_existing_patients`; a load of more than 200 is spread over minutes so new leads never wait | tested - plan `test_bulk_first_load...` |

## B. Who is who (identity)

| ID | Situation | What happens | Status / tests |
| --- | --- | --- | --- |
| E-18 | Contact already in Keap (same email + first + last name) | Reused, not duplicated; the clinic's own tags, fields and **contact type** are untouched | tested - sync, chain (fake), e2e |
| E-19 | Family: two people share an email | Two separate contacts (email alone is never enough) | tested - chain, sync, e2e |
| E-20 | Same name + email but Stride cannot link them to the lead (different DOB/phone) | The second row is held `needs_review`; the lead's contact is not disturbed (prevents two rows fighting over one contact's tags). A unique index also forbids two rows with one contact id | tested - chain `test_an_unmatched_patient...` |
| E-21 | Two existing Keap contacts match | `needs_review`, nothing written | tested - sync |
| E-22 | Lead and patient already have different contacts when Stride links them | Both rows `needs_review` (staff merge in Keap); never auto-merged | tested - plan |
| E-23 | Lead and patient had separate rows, then get linked | Merged into the lead's row, its contact id kept | tested - plan |
| E-24 | Stride cannot decide between two leads (ambiguous) | Patient stays unlinked and becomes its own row | covered by E-20 |
| E-25 | Lead deleted on the dashboard | Row kept, Keap contact kept, no further changes | tested - chain `test_a_deleted_lead...` |
| E-26 | Patient deleted in Stride | Keap untouched (row frozen); a never-synced deleted patient is skipped | tested - chain, plan, unit |

## C. Data quality

| ID | Situation | What happens | Status / tests |
| --- | --- | --- | --- |
| E-27 | Only a phone / only an email / only a postal address | Contact created with what exists (address only when there is no email and no phone) | tested - chain, unit |
| E-28 | No email, phone or address | Skipped with the reason "no contact details" until one appears | tested - plan, unit |
| E-29 | One-word Sheet name | Still links to the Stride patient (first name only) | tested - chain |
| E-30 | Accents, apostrophes, `+` in an email | Round-trip unchanged; second pass finds the same contact (no duplicate) | tested - chain (also live) |
| E-31 | Very long names / emails | Probed in the sandbox: Keap **silently cuts** given and family names to 40 characters and accepts a 90-character email, an apostrophe or `+`, and empty first or last names. We plan the 40-character version so syncs are stable and name searches still match | tested - unit `test_names_are_clipped...`; limits verified live |
| E-32 | Stride and the Sheet disagree on email/birthday | Stride wins; the lead's phone wins (the one we call) | tested - plan, unit |
| E-33 | Email changed in Stride | Keap EMAIL1 updated in place (same contact); EMAIL2/3 and phones kept; the old EMAIL1 is replaced, not kept | tested - chain |
| E-34 | Phone changed | PHONE1 replaced, PHONE2+ kept | tested - sync, e2e |

## D. Time

| ID | Situation | What happens | Status / tests |
| --- | --- | --- | --- |
| E-35 | Clinic time zone | Date and time come from the clinic wall time in `practice_settings.stride_location_timezone` (California: `America/Los_Angeles`), so "2:30 PM" stays 2:30 PM across daylight saving | tested - unit, chain (2:30 PM) |
| E-36 | Time passes (the "next" appointment becomes past) | Re-planned automatically with no data change | tested - plan `test_a_time_passing...` |
| E-37 | Keap reads a date back as `...T04:00:00.000Z` | Compared by its first 10 characters. **Wrong by a day if the Keap account's time zone is east of UTC** | RISK - fine for US accounts |

## E. Stride feed

| ID | Situation | What happens | Status / tests |
| --- | --- | --- | --- |
| E-38 | An older file arrives late | Ignored by the import; Keap unchanged | tested - chain |
| E-39 | Many changes between two Keap ticks | Keap only sees the final state | tested - chain |
| E-40 | Keap is slow or down | Sheet, dashboard, Stride import and calls carry on; rows retry with backoff, then heal | tested - chain (outage) |

## F. Keap behaviour and failure handling

| ID | Situation | What happens | Status / tests |
| --- | --- | --- | --- |
| E-41 | 5xx / timeout on a read | Retried inside the call, then the row `retrying` with backoff; 3 attempts then `failed` | tested - sync |
| E-42 | Timeout on **create** (contact exists, answer lost) | Not retried blindly: next attempt searches first and finds it - one contact | tested - sync |
| E-43 | Crash right after create | The contact id is committed before anything else | tested - sync |
| E-44 | 429 / daily budget | Whole tick stops; claimed rows are released without using an attempt; `Retry-After` honoured | tested - sync, chain |
| E-45 | 401 | One refresh + one retry; both rotated tokens saved together | tested - sync |
| E-46 | Refresh refused / still 401 | `reauthorization_required`, rows wait without using attempts, dashboard shows "Reconnect Keap" | tested - sync, status |
| E-47 | Two callers refresh at once | One refresh (row lock) | tested - sync |
| E-48 | Worker dies mid-sync | `syncing` for 15 minutes -> `retrying`; Keap writes are repeatable | tested - sync |
| E-49 | The plan changes while a person is being sent | Ends `pending` again, not wrongly `synced` | tested - sync |
| E-50 | Two keap-workers started by mistake | The second refuses to run (advisory lock) | tested - sync `test_a_second_worker_cannot_start` |
| E-51 | Contact deleted by staff in Keap | 404 -> search -> reuse or recreate | tested - sync, chain (fake) |
| E-52 | Wrong tag/field id in the mapping | `failed`: "tag N not found in Keap" / "field N not found" | tested - sync |
| E-53 | Field type we cannot format | `failed`: "unsupported field type X for field N" | tested - sync |
| E-54 | Keap's custom-field PATCH semantics | Merged by id; `null` clears; the clinic's own fields are never sent | tested - sync, chain, e2e (verified live) |
| E-55 | Keap's duplicate option | Never used (it merged a family member in the sandbox) | tested - sync `test_a_create_never_asks...` |

## G. Operations

| ID | Situation | What happens | Status / tests |
| --- | --- | --- | --- |
| E-56 | Sandbox ids used against production (or the reverse) | Tag/field ids differ per Keap account. The connection stores the account name, but **nothing checks the mapping belongs to the connected account** | **GAP** - a wrong id either fails ("not found") or silently tags something else |
| E-57 | Staff change tags/fields by hand in Keap | Our owned tags are re-written only when our plan for that person changes; a manual removal of Pending is **not repaired** until then | **GAP**, pinned by chain `test_drift_made_by_hand...` - proposal: a slow nightly check that re-reads each contact (costs requests) |
| E-58 | Staff edit the Appointment Date field in Keap | Overwritten at the next change (we own it) | by design |
| E-59 | Test leads | Excluded unless `KEAP_INCLUDE_TEST_LEADS=true` (sandbox only) | tested - plan |
| E-60 | Personal data in logs | httpx request URLs carry the email/phone (URL-encoded, invisible to redaction); httpx logging is turned to WARNING | tested - sync `test_logs_never_contain...` |
| E-61 | Switched off / not connected / go-live not set | Worker idles and says why on `/dashboard/keap-sync`; planning marks "keap sync not started" | tested - sync, plan |
