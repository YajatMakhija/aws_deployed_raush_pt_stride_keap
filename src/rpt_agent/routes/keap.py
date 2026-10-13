"""Keap OAuth: a super admin connects the practice's Keap account once."""

import logging
from html import escape

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from ..config import get_settings
from ..services import keap_oauth
from ..services.keap_oauth import KeapAuthError
from .dashboard import Actor, _require_super_admin

router = APIRouter(prefix="/api/v1/keap", tags=["keap"])
log = logging.getLogger(__name__)


def _page(message: str, status: int = 200) -> HTMLResponse:
    # Static text only: nothing from the request is echoed back (a code or token must
    # never appear on a page, in a log or in a browser history entry).
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>Keap</title><p>{escape(message)}</p>",
        status_code=status,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.get("/oauth/start")
def oauth_start(actor: Actor):
    _require_super_admin(actor)
    settings = get_settings()
    if not settings.keap_client_id or not settings.keap_client_secret:
        raise HTTPException(status_code=503, detail="Keap app credentials are not configured")
    return {"authorize_url": keap_oauth.authorize_url(keap_oauth.make_state(actor.practice_id))}


@router.get("/oauth/callback")
def oauth_callback(code: str = "", state: str = "", error: str = ""):
    """Keap sends the browser here; it is public, so the signed `state` is the only gate."""
    try:
        practice_id = keap_oauth.practice_from_state(state)
    except Exception:  # noqa: BLE001 - any bad or expired state is just rejected
        return _page("This link has expired. Start the Keap connection again.", 400)
    if error or not code:
        return _page("Keap did not approve the connection. Start again.", 400)
    try:
        account = None
        keap_oauth.exchange_code(practice_id, code, keap_account=account)
    except KeapAuthError:
        return _page("Keap rejected the connection. Check the app credentials and try again.", 400)
    except (ConnectionError, httpx.HTTPError):
        log.warning("keap_oauth_exchange_failed", extra={"event": "keap_oauth_exchange_failed"})
        return _page("Keap could not be reached. Try again in a minute.", 502)
    return _page("Keap connected. You can close this tab.")
