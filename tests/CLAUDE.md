# tests — pytest suite

Run from the repo root:

```bash
~/.local/bin/uv run --frozen python -m pytest -q
~/.local/bin/uv run --frozen ruff check src tests
```

Most tests need no database or network: they fake the psycopg connection (small `Connection` / `_Rows`
classes that match on SQL text) and monkeypatch provider clients. Follow that pattern for new tests.

Optional suites, skipped by default:
- `test_database_integration.py`, `test_number_block_integration.py`, `test_stride_import_integration.py` — need
  `TEST_DATABASE_URL` (a disposable Postgres with migrations applied, never production). Without Docker, the
  `pgserver` pip package runs an embedded Postgres 16; it lacks `pgcrypto`, which migration 001 only creates
  (`gen_random_uuid` is built in), so register an empty `pgcrypto` extension in that install first.
- `test_real_provider_contracts.py` — needs `RUN_REAL_PROVIDER_TESTS=1` and real sandbox credentials.

| File | Covers |
| --- | --- |
| `test_worker.py` | Cadence materialization, claim rules, dispatch, sweepers |
| `test_dashboard.py` | Dashboard API: roles, board stages, cadence versions, activity feed, lead create/delete |
| `test_n8n_integration.py` | Sheet actions (start/restart/DNC/booked, cooldown), profile sync, Sheet snapshots |
| `test_delivery.py`, `test_unspoken_call.py`, `test_last_decision.py` | Vapi end-of-call settlement and outcomes |
| `test_review_pause.py`, `test_callback_timing.py` | Review/pause behaviour, callback scheduling |
| `test_security.py` | Dashboard headers, HMAC, Vapi/Twilio auth |
| `test_api_contract.py`, `test_vapi_contract.py`, `test_vapi_tool_config.py` | HTTP and Vapi tool contracts |
| `test_availability_offers.py`, `test_providers.py`, `test_mock_server.py` | Booking slots, provider clients, mocks |
| `test_agent.py` | Staff assistant |
| `test_stride_import.py`, `test_stride_import_integration.py` | Stride CSV parsing; import edge cases and lead booking/cancel on a real schema (`TEST_DATABASE_URL`) |
| others | parsing, retry, observability, usage report, fixtures, test-lead reset |

Every bug fix should add a regression test here that fails without the fix.
