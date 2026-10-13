"""Keap OAuth: connect once, keep the tokens fresh.

Keap rotates the refresh token on every refresh and the old one stops working, so a
refresh that is not saved locks the practice out. Every function here therefore
works in its own short transaction (never the caller's, which could roll back) and
refreshes under a row lock, so two callers cannot race and burn the refresh token.
Tokens live only in keap_connections: never log or return them.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from ..config import Settings, get_settings
from ..db import transaction
from ..security import sign_slot, verify_slot

REFRESH_WHEN_LEFT = timedelta(hours=3)
STATE_TTL_SECONDS = 600


class KeapAuthError(RuntimeError):
    """The connection needs a person: reconnect Keap from the dashboard."""


def authorize_url(state: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    query = urlencode({
        "client_id": settings.keap_client_id,
        "redirect_uri": settings.keap_redirect_uri,
        "response_type": "code",
        "scope": "full",
        "state": state,
    })
    return f"{settings.keap_authorize_url}?{query}"


def make_state(practice_id: int) -> str:
    return sign_slot(f"keap-oauth:{practice_id}", int(time.time()) + STATE_TTL_SECONDS)


def practice_from_state(state: str) -> int:
    payload, expires = verify_slot(state)  # raises on a bad signature
    if expires < time.time() or not payload.startswith("keap-oauth:"):
        raise ValueError("expired or invalid state")
    return int(payload.split(":", 1)[1])


def _token_request(
    form: dict[str, str], settings: Settings, client: httpx.Client, *, basic: bool
) -> dict[str, Any]:
    auth = (settings.keap_client_id, settings.keap_client_secret) if basic else None
    try:
        response = client.post(
            settings.keap_token_url,
            data=form,
            auth=auth,
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        raise ConnectionError("Keap token request failed") from exc
    if response.status_code in {400, 401}:
        raise KeapAuthError(f"Keap rejected the token request (HTTP {response.status_code})")
    if response.status_code != 200:
        raise ConnectionError(f"Keap token endpoint returned HTTP {response.status_code}")
    try:
        tokens = response.json()
    except ValueError as exc:
        raise ConnectionError("Keap token response was not JSON") from exc
    for key in ("access_token", "refresh_token"):
        if not isinstance(tokens.get(key), str) or not tokens[key]:
            raise ConnectionError("Keap did not return a token pair")
    if not isinstance(tokens.get("expires_in"), (int, float)) or tokens["expires_in"] <= 0:
        raise ConnectionError("Keap did not return a valid expires_in")
    return tokens


def exchange_code(
    practice_id: int,
    code: str,
    *,
    keap_account: str | None = None,
    client: httpx.Client | None = None,
    settings: Settings | None = None,
) -> None:
    """Trade the one-time authorization code for tokens and save them."""
    settings = settings or get_settings()
    client = client or httpx.Client(timeout=settings.request_timeout_seconds)
    tokens = _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.keap_redirect_uri,
            "client_id": settings.keap_client_id,
            "client_secret": settings.keap_client_secret,
        },
        settings, client, basic=False,
    )
    expires_at = datetime.now(UTC) + timedelta(seconds=tokens["expires_in"])
    with transaction() as conn:
        conn.execute(
            "insert into keap_connections(practice_id,keap_account,access_token,refresh_token,"
            "access_token_expires_at,status,last_refreshed_at,last_error) "
            "values(%s,%s,%s,%s,%s,'active',now(),null) "
            "on conflict(practice_id) do update set keap_account=coalesce(excluded.keap_account,"
            "keap_connections.keap_account),access_token=excluded.access_token,"
            "refresh_token=excluded.refresh_token,"
            "access_token_expires_at=excluded.access_token_expires_at,status='active',"
            "last_refreshed_at=now(),last_error=null",
            (practice_id, keap_account, tokens["access_token"], tokens["refresh_token"], expires_at),
        )
    if keap_account is None:
        _label_account(practice_id, tokens["access_token"], settings, client)


def _label_account(
    practice_id: int, token: str, settings: Settings, client: httpx.Client
) -> None:
    """Best effort: remember which Keap account this is, so sandbox and production never mix."""
    try:
        response = client.get(
            settings.keap_api_base_url.rstrip("/") + "/businessProfile",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        tenant = response.headers.get("x-keap-tenant-id", "").strip()
    except httpx.HTTPError:
        return
    if tenant:
        with transaction() as conn:
            conn.execute(
                "update keap_connections set keap_account=%s where practice_id=%s",
                (tenant[:100], practice_id),
            )


def connection_status(practice_id: int) -> dict[str, Any] | None:
    """The non-secret facts about the connection (no tokens)."""
    with transaction() as conn:
        return conn.execute(
            "select keap_account,status,access_token_expires_at,last_refreshed_at,last_error "
            "from keap_connections where practice_id=%s",
            (practice_id,),
        ).fetchone()


def mark_reauthorization_required(practice_id: int, reason: str) -> None:
    with transaction() as conn:
        conn.execute(
            "update keap_connections set status='reauthorization_required',last_error=%s "
            "where practice_id=%s",
            (reason[:200], practice_id),
        )


def _refresh_locked(
    conn, row: dict[str, Any], practice_id: int, settings: Settings, client: httpx.Client
) -> str:
    tokens = _token_request(
        {"grant_type": "refresh_token", "refresh_token": row["refresh_token"]},
        settings, client, basic=True,
    )
    expires_at = datetime.now(UTC) + timedelta(seconds=tokens["expires_in"])
    conn.execute(
        "update keap_connections set access_token=%s,refresh_token=%s,access_token_expires_at=%s,"
        "last_refreshed_at=now(),last_error=null where practice_id=%s",
        (tokens["access_token"], tokens["refresh_token"], expires_at, practice_id),
    )
    return tokens["access_token"]


def access_token(
    practice_id: int,
    *,
    client: httpx.Client | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
    force: bool = False,
    stale_token: str | None = None,
) -> str:
    """A valid access token, refreshed first when under three hours remain.

    `force` (after a 401) refreshes even if the clock says the token is fine, unless
    another caller already replaced `stale_token` while we waited for the row lock.
    """
    settings = settings or get_settings()
    client = client or httpx.Client(timeout=settings.request_timeout_seconds)
    now = now or datetime.now(UTC)
    with transaction() as conn:
        row = conn.execute(
            "select access_token,refresh_token,access_token_expires_at,status "
            "from keap_connections where practice_id=%s for update",
            (practice_id,),
        ).fetchone()
        if row is None:
            raise KeapAuthError("Keap is not connected")
        if row["status"] != "active":
            raise KeapAuthError("Keap needs to be reconnected")
        fresh = row["access_token_expires_at"] - now > REFRESH_WHEN_LEFT
        replaced = force and stale_token is not None and row["access_token"] != stale_token
        if (fresh and not force) or replaced:
            return row["access_token"]
        try:
            return _refresh_locked(conn, row, practice_id, settings, client)
        except KeapAuthError as exc:
            # Saved before raising: the exception must not roll the flag back.
            conn.execute(
                "update keap_connections set status='reauthorization_required',last_error=%s "
                "where practice_id=%s",
                (str(exc)[:200], practice_id),
            )
            failure = exc
    raise failure
