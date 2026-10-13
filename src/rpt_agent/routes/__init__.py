from .appointments import router as appointments_router
from .availability import router as availability_router
from .booking_tools import router as booking_tools_router
from .dashboard import router as dashboard_router
from .health import router as health_router
from .keap import router as keap_router
from .leads import router as leads_router
from .n8n import router as n8n_router
from .twilio import router as twilio_router
from .vapi import router as vapi_router

__all__ = [
    "appointments_router",
    "availability_router",
    "booking_tools_router",
    "dashboard_router",
    "health_router",
    "keap_router",
    "leads_router",
    "n8n_router",
    "twilio_router",
    "vapi_router",
]
