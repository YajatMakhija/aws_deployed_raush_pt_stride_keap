# scripts — developer and ops utilities

| File | Purpose |
| --- | --- |
| `dev.sh` | Local stack: `setup`, `migrate`, `verify`, `seed`, `start` (Docker Compose + mocks), `test`, `demo`. |
| `wait_for_services.py` | Polls local API/mock health after `dev.sh start`. |
| `sync_vapi.py` | Idempotently pushes `config/vapi_tools.json` and the assistant prompt to the configured Vapi assistant. Changes the live assistant; confirm before running against production. |
| `fake_stride.py` | Local stand-in for Stride: `server` (SFTP on 127.0.0.1:2222, user `stride`, files to `local/stride/incoming`), `feed` (full load then a delta every `--every` seconds, synthetic data with every edge case), `seed-leads` (synthetic Sheet leads, local DB only). Run with `uv run --with asyncssh`. |
| `keap_check.py` | Read-only: what we planned for a person in Keap and what the Keap contact really holds (`--patient`, `--status`). |
| `test_booking_call.py` | One real test call with the booking assistant (creates and deletes a test lead). |
| `voice_tests/` | Booking agent tests: `cases.py` (14 scenarios + checks), `bench_booking.py` (chat benchmark with a simulated patient, compares models, no phone calls; bookings hit the Stride demo), `call_booking.py` (real calls to your phone, same cases + timing). Results in `voice_tests/results/` (git-ignored). |
| `sheet_load_test.py` | Direct Sheet sync under load: N pasted Start rows through the real code, an in-memory Sheet and a LOCAL Postgres (refuses any other DB); checks exactly-once and reports timings. No provider is called. |
| `generate_test_usage_report.py` | Builds the client test-usage cost report (`usage_report.py`). |
| `make-demo-env.sh` | Runs on the demo EC2: builds `.env` from `~/old-demo.env` (old host → `demo.aibolt.ai`, Keap redirect, Sheet/Stride/Keap sync off for a safe start). Prints names only, never values. |
