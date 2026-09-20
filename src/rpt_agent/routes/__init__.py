from .appointments import router as appointments_router
from .availability import router as availability_router
from .dashboard import router as dashboard_router
from .health import router as health_router
from .leads import router as leads_router
from .n8n import router as n8n_router
from .twilio import router as twilio_router
from .vapi import router as vapi_router

__all__ = [
    "appointments_router",
    "availability_router",
    "dashboard_router",
    "health_router",
    "leads_router",
    "n8n_router",
    "twilio_router",
    "vapi_router",
]
