from uuid import uuid4

from fastapi import FastAPI, Request

from .agent.router import router as agent_router
from .config import get_settings
from .observability import configure_logging, trace_id_var
from .routes import (
    appointments_router,
    availability_router,
    dashboard_router,
    health_router,
    leads_router,
    twilio_router,
    vapi_router,
)

configure_logging("api")

DESCRIPTION = """
Patient outreach and live Stride booking for Rausch Physical Therapy.

**Authentication.** The three tool endpoints need the Vapi shared secret. Click
**Authorize**, paste `VAPI_WEBHOOK_SECRET`, and Try it out will send it. `Authorization:
Bearer <secret>` works too.

**Dual format.** Every endpoint accepts either the flat JSON shown here or the Vapi
tool-call envelope, so the same URL serves both Swagger and the voice agent.

**Live providers.** Stride, Vapi and Twilio are in real mode. `check-availability` is
read-only; `create-appointment` books a real appointment and queues an SMS.

Provider callbacks (Twilio and Vapi webhooks) are authenticated by request signature and
are deliberately not listed here - they cannot be exercised from this page.
"""

TAGS = [
    {"name": "availability", "description": "Read live Stride openings. Safe."},
    {"name": "appointments", "description": "Create a real Stride appointment."},
    {"name": "leads", "description": "Record what happened on a call."},
    {"name": "system", "description": "Health checks."},
]

# The schema names every dashboard route and its shape. That is a map worth
# having for local work and worth withholding in production, where the same
# surface was reachable unauthenticated at /docs.
_PUBLISH_SCHEMA = get_settings().app_env.lower() not in {
    "preproduction", "preprod", "staging", "production", "prod",
}

app = FastAPI(
    title="RPT Agent API",
    description=DESCRIPTION,
    version="1.0.0-rc1",
    openapi_tags=TAGS,
    docs_url="/docs" if _PUBLISH_SCHEMA else None,
    redoc_url="/redoc" if _PUBLISH_SCHEMA else None,
    openapi_url="/openapi.json" if _PUBLISH_SCHEMA else None,
)
app.include_router(health_router)
app.include_router(availability_router)
app.include_router(dashboard_router)
app.include_router(agent_router)
app.include_router(appointments_router)
app.include_router(leads_router)
app.include_router(vapi_router)
app.include_router(twilio_router)


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    trace_id = request.headers.get("x-trace-id") or uuid4().hex
    token = trace_id_var.set(trace_id)
    try:
        response = await call_next(request)
        response.headers["X-Trace-ID"] = trace_id
        return response
    finally:
        trace_id_var.reset(token)
