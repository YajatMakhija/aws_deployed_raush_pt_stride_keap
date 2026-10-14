# agent — read-only staff assistant

Answers staff questions about up to a few selected leads. It has no write or provider tools.

| File | Purpose |
| --- | --- |
| `router.py` | `POST /api/v1/dashboard/assistant` (JSON) and `/assistant/stream` (SSE). Dashboard auth, per-actor rate limit via a Postgres advisory lock, durable audit in `dashboard_audit_log`. |
| `service.py` | Loads redacted lead context (`load_lead_context`), refuses questions about unselected leads, calls the configured OpenAI-compatible model (`ASSISTANT_BASE_URL`, `ASSISTANT_MODEL`, default gpt-5.6-luna) through LangChain, streams answers. |
| `terminal.py` | `rpt agent` terminal client that calls the streaming endpoint with dashboard headers. |

## Rules

- Disabled unless `ASSISTANT_ENABLED=true`. In a deployed `APP_ENV`, readiness fails until `ASSISTANT_PHI_APPROVED=true` and
  `ASSISTANT_PHI_APPROVAL_REFERENCE` are set. Do not set these just to pass readiness; they record a real vendor
  approval for patient data.
- Only explicitly selected, redacted lead fields may be sent to the model. Keep redaction in `service.py`
  when adding context fields.
- LangSmith/LangChain tracing must stay off in production.
- Tests: `tests/test_agent.py`.
