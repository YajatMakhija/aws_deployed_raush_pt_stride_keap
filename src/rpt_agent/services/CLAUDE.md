# services — business workflows and provider clients

Routes and the worker call into these. Each workflow owns its SQL and runs inside the caller's
`transaction()` connection unless noted.

## Workflows

| File | What it owns |
| --- | --- |
| `lead_actions.py` | Google Sheet commands from n8n: `execute_lead_action()` (start / restart / DNC / booked, idempotent on request ID via `lead_action_requests`) and `sync_sheet_lead()` (profile sync; name/phone changes are flagged for review, never overwritten). `_restart_cadence` has a 10-min cooldown. |
| `lead_status.py` | Lead/call state machine: `apply_call_outcome()`, `report_lead_status()`, callbacks clamped to business hours, explicit opt-outs, `mark_booked()`, status history. |
| `delivery.py` | Settles provider results: Vapi end-of-call reports (`process_vapi_end_report`, transcripts, structured outcome), Twilio delivery status, outbox processing (confirmation SMS, Keap handoff), reprocessing of failed webhooks. |
| `booking.py` | `BookingService`: Stride availability and appointment creation, patient/case creation, idempotency, confirmation SMS. Gated by `practice_settings.stride_booking_enabled`. |
| `slot_booking.py` | Booking engine for the voice agent: `find_slots()` (lead → location → case type → clinicians → 3 nearest cached slots, remembered per call) and `book_slot()` (hold → live re-check → patient → case → appointment). See `docs/BOOKING_ENGINE.md`. |
| `availability_sync.py` | Refreshes the `availability_slots` cache from Stride per location + visit length; `booking_now()` honours `BOOKING_TODAY_OVERRIDE`. |
| `lead_stage.py` | `stage_of()` (board column, used by `routes/dashboard.py` as `_stage`) and `sheet_lead_status()` (the Sheet's Lead Status mirrors the column: New / In cadence / Paused / Needs Review / Booked / Do not outreach / Closed). |
| `review.py` | `skip_remaining_planned` / `restore_pause_skipped` (pause/resume mechanics), `flag_lead_for_review`, `hand_over_number` (one active lead per phone; new lead inherits opt-outs). |
| `number_block.py` | Dashboard Do not contact switch: `block_number()` (same effect as the Sheet command) and `unblock_number()` (admin only; must choose `continue` or `restart`, clears opt-outs on the number, tells the Sheet). |
| `stride_import.py` | Parses one Stride export file and applies it to `stride_*` tables: Upsert/Delete tombstones, newer-wins on Modified Date Time, appointment Deletes matched by case + start, PHI-free row issues. |
| `stride_leads.py` | Links Stride patients to leads (name + DOB, or name + phone), marks a lead booked when Stride shows a future appointment, flags it for review when that booking is cancelled. Recomputed each tick. |
| `appointment_views.py` | Read-only booking records for the dashboard (calendar, lead booking card, home Next appointments, booked board cards, assistant context): merges our `appointments` with `stride_appointments` (Stride wins once imported), normalises Stride statuses, tags each visit Elliot / Sheet lead / Stride only, and finds the call moment for Elliot bookings. |
| `google_sheet.py` | Google Sheet access (gspread, service account): whole-tab read, columns by header text, queued cell writes that re-check each row's identity (Lead ID, Action Request ID, Name, Phone) just before writing. |
| `sheet_direct.py` | Direct Sheet sync (replaces the n8n workflows): command rules and labels, claim → `execute_lead_action`, stuck-row recovery with the same request ID, profile sync by fingerprint (`sheet_row_state`), snapshot → row. |
| `sheet_sync.py` | Builds the Sheet snapshot (`build_sheet_snapshot`) and enqueues it (`enqueue_sheet_update` → `integration_outbox`). Omits `action_status` unless booked / DNC / completed so n8n keeps the existing cell. |

## Provider clients

| File | Provider |
| --- | --- |
| `provider_http.py` | `ProviderService` base: bounded timeouts, retries, `ProviderError` classification. All provider clients extend it. |
| `vapi_service.py` | Vapi outbound call creation. |
| `twilio_service.py` | Twilio message send (uses `TWILIO_FROM_NUMBER` from `.env`). |
| `stride_service.py` | Stride HTTP contract (availability `Slot`s, patients, cases, appointments). |
| `keap_service.py` | Signed `appointment.booked.v1` handoff to the Keap team webhook. |

## Notes

- Anything that skips or creates `outreach_events` changes who gets called; add a test in `tests/`.
- Sheet-facing response codes (`*_needs_review`, `restart_too_soon`, `lead_already_exists`, …) are mapped to
  Sheet text by the n8n workflows; keep `plan.md` in sync when adding one.
- Provider mode (real/mock) is chosen in config; never branch business logic on it.
