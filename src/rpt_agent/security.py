from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request
from fastapi.security import APIKeyHeader

from .config import Settings, get_settings

# Documentation only: makes Swagger's Authorize button send the header that
# require_vapi_auth() checks. auto_error=False so it never enforces on its own.
vapi_secret_scheme = APIKeyHeader(
    name="x-vapi-secret",
    auto_error=False,
    description="Paste VAPI_WEBHOOK_SECRET to use Try it out.",
)

MAX_N8N_INTAKE_BODY_BYTES = 64 * 1024


@dataclass(frozen=True)
class DashboardActor:
    user_id: str
    email: str


def timestamped_hmac(secret: str, timestamp: str, body: bytes) -> str:
    """Sign an exact HTTP body with a replay-protected timestamp."""
    return hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()


def _fresh_timestamp(timestamp: str, *, max_age_seconds: int = 300) -> bool:
    try:
        return abs(int(time.time()) - int(timestamp)) <= max_age_seconds
    except (TypeError, ValueError):
        return False


async def require_n8n_intake_auth(request: Request) -> None:
    """Authenticate the n8n intake caller before parsing any patient data."""
    settings = get_settings()
    # Local testing only. Never honor this flag in production/preproduction.
    if settings.n8n_intake_auth_disabled:
        if settings.app_env.lower() in {
            "production",
            "prod",
            "preproduction",
            "preprod",
            "staging",
        }:
            raise HTTPException(
                status_code=503,
                detail="N8N_INTAKE_AUTH_DISABLED cannot be used outside local development",
            )
        return
    if not settings.n8n_intake_key_id or not settings.n8n_intake_secret:
        raise HTTPException(status_code=503, detail="n8n intake is not configured")
    key_id = request.headers.get("x-rpt-key-id", "")
    timestamp = request.headers.get("x-rpt-timestamp", "")
    signature = request.headers.get("x-rpt-signature", "").removeprefix("sha256=")
    if not hmac.compare_digest(key_id, settings.n8n_intake_key_id):
        raise HTTPException(status_code=401, detail="invalid n8n credentials: key id mismatch")
    if not signature:
        raise HTTPException(status_code=401, detail="invalid n8n credentials: missing signature")
    if not _fresh_timestamp(timestamp):
        raise HTTPException(
            status_code=401,
            detail="invalid n8n credentials: timestamp expired or invalid (must be within 5 minutes)",
        )
    content_length = request.headers.get("content-length", "")
    if content_length.isdigit() and int(content_length) > MAX_N8N_INTAKE_BODY_BYTES:
        raise HTTPException(status_code=413, detail="n8n request body is too large")
    body = await request.body()
    if len(body) > MAX_N8N_INTAKE_BODY_BYTES:
        raise HTTPException(status_code=413, detail="n8n request body is too large")
    expected = timestamped_hmac(settings.n8n_intake_secret, timestamp, body)
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(
            status_code=401,
            detail="invalid n8n credentials: bad signature (secret mismatch or body changed after signing)",
        )

def n8n_sheet_headers(
    body: bytes, *, timestamp: str | None = None, settings: Settings | None = None
) -> dict[str, str]:
    """Build the signed headers used by the AWS Sheet worker."""
    settings = settings or get_settings()
    timestamp = timestamp or str(int(time.time()))
    return {
        "Content-Type": "application/json",
        "X-RPT-Key-Id": settings.n8n_sheet_key_id,
        "X-RPT-Timestamp": timestamp,
        "X-RPT-Signature": f"sha256={timestamped_hmac(settings.n8n_sheet_webhook_secret, timestamp, body)}",
    }


def require_dashboard_auth(request: Request) -> DashboardActor:
    settings = get_settings()
    expected = settings.dashboard_api_token
    supplied = request.headers.get("x-dashboard-token", "")
    if not expected:
        raise HTTPException(status_code=503, detail="dashboard API is not configured")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid dashboard credentials")
    user_id = request.headers.get("x-dashboard-user-id", "").strip()
    email = request.headers.get("x-dashboard-user-email", "").strip().lower()
    if not user_id or not email or len(user_id) > 200 or len(email) > 320:
        raise HTTPException(status_code=401, detail="authenticated dashboard user is required")
    return DashboardActor(user_id=user_id, email=email)


async def require_vapi_auth(request: Request) -> None:
    settings = get_settings()
    expected = settings.vapi_webhook_secret
    supplied = request.headers.get("authorization", "")
    token = supplied[7:] if supplied.lower().startswith("bearer ") else ""
    legacy_secret = request.headers.get("x-vapi-secret", "")
    if expected and (
        hmac.compare_digest(token, expected) or hmac.compare_digest(legacy_secret, expected)
    ):
        return
    timestamp = request.headers.get("x-vapi-timestamp", "")
    signature = request.headers.get("x-vapi-signature", "")
    try:
        fresh = _fresh_timestamp(timestamp)
    except ValueError:
        fresh = False
    if settings.vapi_hmac_secret and signature and fresh:
        body = await request.body()
        expected_signature = hmac.new(
            settings.vapi_hmac_secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(signature.removeprefix("sha256="), expected_signature):
            return
    if expected:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - malformed JSON remains unauthenticated
            body = {}
        candidates = [body.get("vapi_secret")] if isinstance(body, dict) else []
        message = body.get("message") if isinstance(body, dict) else None
        message = message if isinstance(message, dict) else {}
        for key in ("toolCallList", "toolCalls"):
            for item in message.get(key) or []:
                if not isinstance(item, dict):
                    continue
                function = item.get("function") if isinstance(item.get("function"), dict) else {}
                arguments = item.get("arguments") or item.get("parameters") or function.get("arguments")
                if isinstance(arguments, str):
                    try:
                        import json

                        arguments = json.loads(arguments)
                    except (TypeError, ValueError):
                        arguments = {}
                if isinstance(arguments, dict):
                    candidates.append(arguments.get("vapi_secret"))
        if any(
            isinstance(candidate, str) and hmac.compare_digest(candidate, expected)
            for candidate in candidates
        ):
            return
    raise HTTPException(status_code=401, detail="invalid Vapi credentials")


async def require_twilio_auth(request: Request, form: dict[str, str]) -> None:
    """Validate Twilio's HMAC-SHA1 request signature."""
    settings = get_settings()
    auth_token = settings.twilio_auth_token
    signature = request.headers.get("x-twilio-signature", "")
    if settings.mode("twilio") == "mock" and signature == "mock-valid":
        return
    if not auth_token or not signature:
        raise HTTPException(status_code=401, detail="missing Twilio signature")
    if settings.public_base_url:
        public_url = f"{settings.public_base_url.rstrip('/')}{request.url.path}"
        if request.url.query:
            public_url = f"{public_url}?{request.url.query}"
    else:
        public_url = str(request.url)
    data = public_url + "".join(f"{key}{form[key]}" for key in sorted(form))
    digest = base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()
    if not hmac.compare_digest(digest, signature):
        raise HTTPException(status_code=401, detail="invalid Twilio signature")


def sign_slot(payload: str, expires_at: int) -> str:
    content = f"{payload}|{expires_at}"
    signature = hmac.new(
        get_settings().slot_token_secret.encode(), content.encode(), hashlib.sha256
    ).hexdigest()
    return base64.urlsafe_b64encode(f"{content}|{signature}".encode()).decode().rstrip("=")


def verify_slot(token: str) -> tuple[str, int]:
    try:
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        payload, expires, signature = decoded.rsplit("|", 2)
        content = f"{payload}|{expires}"
        expected = hmac.new(
            get_settings().slot_token_secret.encode(), content.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected) or int(expires) < int(time.time()):
            raise ValueError
        return payload, int(expires)
    except Exception as exc:
        raise ValueError("invalid or expired slot token") from exc


def sign_handoff(body: bytes) -> str:
    return hmac.new(get_settings().keap_handoff_secret.encode(), body, hashlib.sha256).hexdigest()
