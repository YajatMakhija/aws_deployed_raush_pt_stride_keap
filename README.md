# RPT Outreach Agent

Pre-production Python service for the Rausch PT 14-day outreach cadence. Vapi, Twilio, Stride, Supabase,
and the Keap-team handoff have real provider adapters; mocks remain available only through an explicit
Compose profile for automated/local testing. Production cadence spreading is unchanged.

## Project layout

```text
src/rpt_agent/
  api.py                 FastAPI assembly and trace middleware
  agent/                 read-only selected-lead assistant and terminal client
  routes/
    availability.py      POST /api/v1/tools/check-availability
    appointments.py      POST /api/v1/tools/create-appointment
    leads.py             POST /api/v1/webhooks/vapi/lead-status
    vapi.py              Compatibility tool endpoint and durable end report
    twilio.py             Signed inbound/status callbacks
  services/
    booking.py            Stride booking workflow and idempotency
    lead_status.py        Lead/call transitions and duplicate guard
    delivery.py           SMS/outbox/webhook delivery
    stride_service.py     Stride HTTP contract
    vapi_service.py       Vapi outbound-call contract
    twilio_service.py     Twilio message contract
    keap_service.py       Signed Keap-team handoff contract
    provider_http.py      Shared bounded HTTP/retry behavior
  providers.py           Small compatibility facade over provider services
  worker.py              Cadence claim, dispatch, settlement, and sweepers
  mock_server.py         Deterministic Stride/Twilio/Keap/Vapi fixtures
  usage_report.py        Real test-provider cost ledger refresh and client report
  config.py              Environment-driven runtime configuration
  db.py                  Hosted Supabase Postgres pool and transactions
supabase/migrations/      Ordered, idempotent schema migrations
config/                   Vapi tool schemas and assistant prompt
scripts/                  Local start and Vapi synchronization utilities
tests/                    Contract, unit, scenario, and optional DB tests
```

This keeps the useful API/service/config separation from the reference repository without its duplicated
schedulers, fixed timezone offsets, raw request logging, or AWS-only runtime complexity.

## First local start

1. Copy `.env.example` to `.env` and fill every blank/placeholder from the pre-production secret store.
2. Use the real provider profile:

   ```dotenv
   APP_ENV=preproduction
   VAPI_MODE=real
   TWILIO_MODE=real
   STRIDE_MODE=real
   KEAP_MODE=real
   TEST_MODE=false
   ```

3. Apply/verify the schema. `rpt seed` is for a fresh sandbox only; it intentionally leaves
   `stride_booking_enabled=false`.

   ```powershell
   docker compose run --rm api rpt migrate
   docker compose run --rm api rpt verify
   ```

4. Start and verify everything:

   ```powershell
   docker compose up --build -d
   Invoke-RestMethod http://localhost:8000/health
   Invoke-RestMethod http://localhost:8000/ready
   docker compose logs -f api worker
   ```

The API docs are at `http://localhost:8000/docs`. JSON step logs are also rotated under `logs/`.
`X-Trace-ID` correlates
API, provider, worker, and webhook activity, and PHI/secrets are redacted.

Before enabling real Stride appointment creation, verify the location ID, clinician IDs, duration, and
IANA booking timezone in `practice_settings`, then set `stride_booking_enabled=true`. Availability remains
usable while this gate is false.

The Stride sandbox appointment-type IDs are confirmed: Follow-up 1451, **Initial Evaluation 1452**,
Progress Note 1453, Reevaluation 1454, Recertification 1455, Consultation 1456, Event 1457, Meeting 1458,
PTO 1459, Lunch 1460. The agent only books first visits, so `stride_appointment_type_id` defaults to 1452
(migration `016`). Production IDs will differ - re-confirm before pointing at `app.stridethera.com`.

Enable booking for the intended practice:

```sql
update public.practice_settings ps
set stride_booking_enabled = true
from public.practices p
where p.id = ps.practice_id and p.slug = 'rausch-pt';
```

## Production staff assistant

The staff assistant is disabled by default. It has no write/provider tools and sends only explicitly selected,
redacted lead context to Kimi. Every authenticated request is durably audited in `dashboard_audit_log`; a
Postgres advisory lock enforces the configured per-actor one-minute request limit across API processes.

Before enabling it for real patients, obtain documented approval for Moonshot to process the intended patient
data and record the non-secret agreement/security-review reference. Then configure:

```dotenv
APP_ENV=production
ASSISTANT_ENABLED=true
ASSISTANT_REQUESTS_PER_MINUTE=10
LANGSMITH_TRACING=false
LANGCHAIN_TRACING_V2=false
MOONSHOT_API_KEY=<from the production secret store>
KIMI_MODEL=kimi-k3
KIMI_PHI_APPROVED=true
KIMI_PHI_APPROVAL_REFERENCE=<contract-or-security-review-reference>
```

Do not set the last two values merely to bypass readiness. Until approval exists, leave the assistant disabled;
the rest of the production API remains available. Apply migration `023`, rebuild, and require readiness before
exposing the service:

```powershell
docker compose -f docker-compose.prod.yml run --rm api rpt migrate
docker compose -f docker-compose.prod.yml up -d --build
Invoke-RestMethod https://stride.aibolt.ai/ready
```

For an authorized operator with server access, open the terminal client inside the API container:

```powershell
docker compose -f docker-compose.prod.yml exec api `
  rpt agent --api-url http://localhost:8000
```

The terminal uses `POST /api/v1/dashboard/assistant/stream` and prints redacted Server-Sent Event chunks as
LangChain receives them from Kimi. The original `POST /api/v1/dashboard/assistant` JSON response remains
available for non-streaming clients.

For deterministic tests only, start the mock provider explicitly:

```powershell
docker compose --profile mock up --build
```

## ngrok and Vapi

Follow [docs/LOCAL_VAPI_NGROK.md](docs/LOCAL_VAPI_NGROK.md). In short: keep Compose running, start
`ngrok http 8000`, set `PUBLIC_BASE_URL`, and run:

```powershell
$env:PYTHONPATH = "src"
python scripts/sync_vapi.py
```

That command idempotently synchronizes the three synchronous tools to their direct endpoints and the
assistant end-report webhook.
It preserves Vapi's built-in transfer/end-call tools. Tool requests accept the current
`message.toolCallList` contract and a narrow legacy compatibility shape; authenticated business failures
still return HTTP 200 with ordered, single-line results using the exact `toolCallId`.

## Consented synthetic cadence test

Do not use an arbitrary or production patient number. Once the owner of a working number has explicitly
consented, create a marked test lead:

```powershell
docker compose run --rm api rpt test-lead `
  --phone "+1XXXXXXXXXX" `
  --first-name "Synthetic" `
  --last-name "Tester" `
  --dob "1990-01-01" `
  --consent-reference "written-test-consent-2026-08-25"
```

With `TEST_MODE=true`, day 0 starts immediately and each cadence day is one minute. Only `is_test=true`
leads bypass production legal-hour/contact-cap gates for this synthetic test. Normal leads keep the
documented schedule. Reusing the same normalized first and last name automatically removes only the older
`is_test=true` / `synthetic_test` lead and its test workflow records before creating the new run. Replacement
is refused while a call is active, and the command requires `TEST_MODE=true`. Monitor with:

```powershell
docker compose logs -f worker api
Get-Content logs/rpt-agent-worker.jsonl -Wait
```

This accelerated synthetic flow is retained only as an explicit development tool. Do not enable it in the
pre-production runtime profile.

## Test usage and client cost report

Every accepted real Vapi call and real Twilio SMS for an `is_test=true` lead is written to the durable
`test_usage_ledger`. Mock operations are excluded. The database retains the protected recipient number so
the audit survives test-lead cleanup, while the shareable report contains only the last four digits and a
stable one-way fingerprint.

Refresh provider statuses/prices and rebuild the client report before sharing it:

```powershell
$env:PYTHONPATH = "src"
python scripts/generate_test_usage_report.py
```

The generated report is [testing_updates/CLIENT_TEST_USAGE.md](testing_updates/CLIENT_TEST_USAGE.md).
Provider invoices remain the accounting source of truth because prices can settle or be adjusted later.

## Commands and verification

- `rpt migrate`, `rpt verify`, `rpt seed` — database lifecycle.
- `rpt test-lead ...` — create a consented, accelerated synthetic lead.
- `rpt tick` — run one worker tick for debugging.
- `rpt demo` — fully mocked demo; intentionally refuses to run if any provider is real.
- `pytest -q` — unit/contract suite. Database tests require `TEST_DATABASE_URL` and are skipped otherwise.
- `ruff check .` — static checks.

Provider modes are independent. Changing `VAPI_MODE`, `TWILIO_MODE`, `STRIDE_MODE`, or `KEAP_MODE`
switches adapters without modifying cadence or booking logic.

The Keap integration is the signed, event-ID-deduplicated webhook owned by the Keap team. It is real when
`KEAP_MODE=real` and `KEAP_HANDOFF_URL` points to that receiver. Direct Keap REST/OAuth contact mutation is
not implemented because no Keap application/OAuth contract was supplied.

## Retry and reconciliation

Transient, safely repeatable provider failures use bounded exponential backoff with jitter. HTTP 429 honors
`Retry-After`; idempotent reads and the event-ID-deduplicated Keap handoff may be retried. Credential,
validation, and other permanent failures stop immediately. Ambiguous Vapi call, Twilio SMS, and Stride
creation results are never blindly repeated because the provider may already have accepted them; those rows
become `unknown` and require provider reconciliation.

Durable retries default to five attempts, starting at 60 seconds and capped at one hour unless a longer
provider `Retry-After` must be honored. Short in-request
retries default to three attempts and are limited to safe operations and brief waits. Configure them with
`RETRY_MAX_ATTEMPTS`, `RETRY_BASE_SECONDS`, `RETRY_MAX_SECONDS`, `HTTP_RETRY_ATTEMPTS`, and
`HTTP_RETRY_BASE_SECONDS`. Run `rpt migrate` before starting the updated worker so the retry,
integration-audit, outcome-source, and Stride booking-gate schema from migrations 013-015 is present.

The `.env` file is git-ignored and excluded from the Docker build context. Since credentials pasted into
chat should be treated as exposed, rotate them before production use and rerun the Vapi sync after updating
the credential in Vapi.
