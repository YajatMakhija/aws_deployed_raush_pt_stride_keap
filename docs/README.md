# docs — what each document is for

| Document | Read it when |
| --- | --- |
| [PHASE2.md](PHASE2.md) | Working on the Stride SFTP import (Phase 1 of the Stride/Keap work): file flow, tables, matching, status page. |
| [PHASE_2A_KEAP_DATA.md](PHASE_2A_KEAP_DATA.md) | Building Phase 2A: what each person's Keap contact should hold (starts with fetching Keap's tags). |
| [PHASE_2B_KEAP_DELIVERY.md](PHASE_2B_KEAP_DELIVERY.md) | Building Phase 2B: sending contacts and tags to Keap (OAuth, rate limits, worker). |
| [BOOKING_ENGINE.md](BOOKING_ENGINE.md) | Working on the voice agent's find-slots / book-appointment tools and the slot cache. |
| [CLIENT_TEST_USAGE.md](CLIENT_TEST_USAGE.md) | Generated client report of test calls/texts and their cost (`scripts/generate_test_usage_report.py`). |
| [FUTURE_DEPLOYMENT.md](FUTURE_DEPLOYMENT.md) | Planning a move to new infrastructure. |
| [LOCAL_VAPI_NGROK.md](LOCAL_VAPI_NGROK.md) | Running the local Docker stack with real Vapi calls through an ngrok tunnel. |
| [REFERENCE_REPOSITORY_REVIEW.md](REFERENCE_REPOSITORY_REVIEW.md) | Background: what was taken from the earlier reference repository. |

The Google Sheet ↔ n8n ↔ backend contract lives in [`../plan.md`](../plan.md); the full project history in
[`../context/PROJECT_CONTEXT.md`](../context/PROJECT_CONTEXT.md).
