# routes — FastAPI routers

Thin HTTP layer: authenticate, validate with pydantic, call a service, shape the response. Business logic
belongs in `services/`. Every router is exported from `__init__.py` and registered in `api.py`.

| File | Prefix | Caller | Endpoints |
| --- | --- | --- | --- |
| `health.py` | — | load balancer / ops | `GET /health` (liveness), `GET /ready` (config + DB + Stride gate) |
| `availability.py` | `/api/v1/tools` | Vapi assistant | `POST /check-availability` — live Stride slots (read-only) |
| `appointments.py` | `/api/v1/tools` | Vapi assistant | `POST /create-appointment` — real Stride booking, idempotent (old single-clinic flow) |
| `booking_tools.py` | `/api/v1/tools` | Vapi assistant | `POST /find-slots`, `POST /book-appointment` — booking engine (see `docs/BOOKING_ENGINE.md`) |
| `leads.py` | `/api/v1/webhooks/vapi` | Vapi assistant | `POST /lead-status` — record a call outcome |
| `vapi.py` | `/api/v1/vapi` | Vapi | `POST /tools` (compat tool endpoint), `POST /webhook` (end-of-call report, stored durably then processed) |
| `twilio.py` | `/api/v1/twilio` | Twilio | `POST /inbound-sms` (replies, STOP), `POST /message-status` (delivery callbacks); signature-checked |
| `n8n.py` | `/api/v1/integrations/n8n` | n8n (Google Sheet) | `POST /lead-actions` (start / restart / DNC / booked), `POST /lead-sync` (profile fields); HMAC + `X-Request-ID` |
| `dashboard.py` | `/api/v1/dashboard` | Next.js proxy | snapshot, lead CRUD, cadence pause/resume, contact rules, board stage moves, review resolve, outreach reschedule, cadence versions/steps, message templates, manual SMS |
| `dashboard_calendar.py` | `/api/v1/dashboard` | Next.js proxy | `GET /appointment-calendar` (every booking, ours and Stride's, via `services/appointment_views.py`; also feeds the Analytics appointment counts), `GET /appointment-slots`, `POST /appointments` (staff booking) |
| `tool_request.py` | — | helper | shared Vapi tool-request authentication |

The staff assistant router (`/api/v1/dashboard/assistant`) lives in `../agent/router.py`.

## dashboard.py essentials (~1800 lines)

- `_stage(row)` decides the board column (new / cadence / attention / booked / closed). Order of checks
  matters: booked → attention → closed statuses → new/pending → paused stays `cadence` → spent cadence = closed.
- `_lead(row)` shapes a lead for the board; `GET /snapshot` returns all leads + metrics; `GET /leads/{id}`
  returns detail with events, calls, messages, appointments, history and the `activity` feed.
- `_audit(...)` writes `dashboard_audit_log` with actor name and lead id; call it for every staff mutation.
- `_require_super_admin` guards global cadence/template writes and deletes (call it before opening a
  transaction).
- `POST /leads/{id}/stage` drives board drag-and-drop (including restart from Day 0 when moved to New).

## When adding a dashboard endpoint

Also add its path to `ALLOWED_PATH` in `rpt_frontend/app/api/dashboard/[[...path]]/route.ts`, or the
frontend proxy rejects it with "dashboard path not allowed".
