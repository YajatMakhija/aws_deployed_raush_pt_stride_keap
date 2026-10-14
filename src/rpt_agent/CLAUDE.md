# src/rpt_agent — application package

Top-level modules. HTTP lives in `routes/`, business workflows in `services/`, the staff assistant in `agent/`.

| Module | Purpose |
| --- | --- |
| `api.py` | FastAPI app: trace middleware, registers every router. Add new routers here. |
| `config.py` | `Settings` (pydantic-settings, reads `.env`) and `get_settings()`. Per-provider `*_MODE` real/mock, readiness validation. New env vars go here and in `.env.example`. |
| `db.py` | Psycopg connection pool and `transaction()` context manager (dict rows). Use it for all SQL. |
| `worker.py` | Cadence worker. `materialize_cadence()` builds a lead's schedule; `CLAIM_SQL` decides what is due (business hours, daily caps, opt-outs, suppression, TEST_MODE); `dispatch_job()` calls Vapi/Twilio; `run_tick()` runs claim → dispatch → sweepers; advisory lock keeps a single dispatcher. |
| `stride_import_worker.py` | Stride SFTP importer: picks finished CSVs from the inbox (oldest first), one transaction per file via `services/stride_import.py`, moves files to archive/error, then `reconcile_leads`. |
| `booking_worker.py` | `rpt-booking-worker` loop: expire slot holds, refresh the slot cache from Stride every `BOOKING_SYNC_SECONDS`, heartbeat. |
| `sheet_sync_worker.py` | Claims `integration_outbox` Sheet events. n8n mode: signs and POSTs snapshots to the n8n webhook. Direct mode (`run_direct_tick`): one Sheet read per poll, commands/recovery/profile sync via `services/sheet_direct.py`, snapshots written into the lead's row, heartbeat in `service_heartbeats`. |
| `security.py` | Auth for every inbound caller: dashboard token + identity headers (`DashboardActor`), n8n HMAC, Vapi secret/HMAC, Twilio signature; signed slot tokens and Keap handoff signing. |
| `vapi_contract.py` | Parses Vapi tool-call payloads and end-of-call reports; maps Vapi ended reasons to call outcomes; builds tool responses. |
| `providers.py` | `ProviderClients` facade bundling the Vapi/Twilio/Stride/Keap services. |
| `observability.py` | JSON logging, PHI/secret redaction, `WorkflowTrace` (trace IDs across API, worker, providers). |
| `cli.py` | `rpt` command: migrate, verify, seed, demo, test-lead, agent. |
| `usage_report.py` | Test-provider cost ledger refresh and client usage report. |
| `mock_server.py` | Deterministic Stride/Twilio/Keap/Vapi mocks (Compose `mock` profile, tests only). |
| `parsing.py`, `retry.py`, `sftp_fixtures.py`, `api_schemas.py` | Small helpers: flexible dates, retry delays, CSV fixtures, OpenAPI-only request models. |

## Notes

- `worker.py` and `CLAIM_SQL` are safety-critical: a wrong condition calls real patients. Change with tests
  in `tests/test_worker.py` and read the surrounding comments first.
- Exactly one cadence worker may run (rate governors are per process; migration 025 enforces the claim
  protocol).
- `get_settings()` is cached (`@cache`); restart processes after `.env` changes.
- The practice DB row (`practice_settings`) overrides some `.env` values, e.g. `vapi_phone_number_id`.
