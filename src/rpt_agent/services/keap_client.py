"""Keap REST v2 client: only the calls the contact sync needs.

Built on ProviderService, so transient errors and short Retry-After 429s are retried
for us and every call leaves a PHI-free integration_events row. Filters carry emails
and phones, so query strings always go through `params=`, never inside the url
(the shared layer logs the url).
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from ..config import Settings
from ..observability import WorkflowTrace, quiet_http_libraries
from . import keap_oauth
from .keap_oauth import KeapAuthError
from .provider_http import ProviderError, ProviderService

quiet_http_libraries()  # Keap search urls carry emails and phones

CONTACT_FIELDS = (
    "id,given_name,family_name,email_addresses,phone_numbers,birth_date,contact_type,"
    "custom_fields,tag_ids"
)
MODEL_TTL_SECONDS = 3600


class KeapRateLimited(RuntimeError):
    """Keap (or our own budget) says stop for a while; retry after `retry_after_seconds`."""

    def __init__(self, retry_after_seconds: int, message: str = "Keap rate limit"):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class KeapRejected(RuntimeError):
    """Keap refused the request itself (bad value, unknown tag or field): retrying cannot help."""

    def __init__(self, call: str, status: int, code: str | None = None):
        super().__init__(f"{call} rejected: {code or status}")
        self.call, self.status, self.code = call, status, code


class RateLimiter:
    """Per-minute sliding window plus a per-day budget (UTC day), in this process."""

    def __init__(
        self,
        per_minute: int,
        per_day: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], str] = lambda: datetime.now(UTC).date().isoformat(),
    ):
        self.per_minute, self.per_day = per_minute, per_day
        self._clock, self._sleep, self._today = clock, sleep, today
        self._recent: deque[float] = deque()
        self._day = today()
        self.used_today = 0
        self._block_until = 0.0

    def _roll_day(self) -> None:
        if self._today() != self._day:
            self._day, self.used_today = self._today(), 0

    def acquire(self) -> None:
        self._roll_day()
        if self.used_today >= self.per_day:
            raise KeapRateLimited(3600, "daily Keap request budget used")
        now = self._clock()
        if now < self._block_until:
            self._sleep(self._block_until - now)
        while True:
            now = self._clock()
            while self._recent and now - self._recent[0] >= 60:
                self._recent.popleft()
            if len(self._recent) < self.per_minute:
                break
            self._sleep(max(0.01, 60 - (now - self._recent[0])))
        self._recent.append(self._clock())
        self.used_today += 1

    def note_headers(self, headers: httpx.Headers) -> None:
        """Keap reports what is left; if any bucket is nearly empty wait out the minute."""
        for name in ("x-keap-product-throttle-available", "x-keap-tenant-throttle-available"):
            try:
                if int(headers.get(name, "")) < 10:
                    self._block_until = self._clock() + 60
            except ValueError:
                continue


class KeapClient(ProviderService):
    def __init__(
        self,
        practice_id: int,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        *,
        limiter: RateLimiter | None = None,
        audit_enabled: bool = True,
    ):
        super().__init__(settings, client, audit_enabled=audit_enabled)
        self.practice_id = practice_id
        self.limiter = limiter or RateLimiter(
            self.settings.keap_max_requests_per_minute, self.settings.keap_max_requests_per_day
        )
        self.stats = {"created": 0, "updated": 0, "tags_applied": 0, "tags_removed": 0}
        self._model: tuple[float, dict[str, str]] | None = None
        # Count every HTTP request that actually goes out, including the shared layer's automatic
        # retries (up to HTTP_RETRY_ATTEMPTS per call), so Keap's per-minute and per-day limits are
        # never quietly exceeded (review of PR #13, 2026-10-14). One hook per httpx client.
        limiter = self.limiter

        def count_request(_request: httpx.Request) -> None:
            limiter.acquire()

        count_request.keap_limiter = True  # type: ignore[attr-defined]
        hooks = dict(self.client.event_hooks)
        hooks["request"] = [h for h in hooks.get("request", []) if not getattr(h, "keap_limiter", False)]
        hooks["request"].append(count_request)
        self.client.event_hooks = hooks

    # --- plumbing ---------------------------------------------------------

    def _call(
        self,
        trace: WorkflowTrace,
        method: str,
        path: str,
        *,
        operation: str,
        params: dict[str, Any] | None = None,
        json: Any = None,
        idempotent: bool | None = None,
    ) -> httpx.Response:
        url = self.settings.keap_api_base_url.rstrip("/") + path
        token = keap_oauth.access_token(self.practice_id, client=self.client, settings=self.settings)
        for attempt in (1, 2):
            response = self._request(
                trace, "keap", method, url,
                operation=operation, idempotent=idempotent, params=params, json=json,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            self.limiter.note_headers(response.headers)
            if response.status_code == 401 and attempt == 1:
                token = keap_oauth.access_token(
                    self.practice_id, client=self.client, settings=self.settings,
                    force=True, stale_token=token,
                )
                continue
            if response.status_code == 401:
                keap_oauth.mark_reauthorization_required(self.practice_id, "Keap rejected the token")
                raise KeapAuthError("Keap rejected the refreshed token")
            break
        if response.status_code == 429:
            raise KeapRateLimited(self._retry_after(response) or 60)
        return response

    @staticmethod
    def _error_code(response: httpx.Response) -> str | None:
        try:
            body = response.json()
            return str(body.get("status") or body.get("code")) if isinstance(body, dict) else None
        except ValueError:
            return None

    def _ok(self, response: httpx.Response, call: str, *, idempotent: bool, ok=(200,)) -> httpx.Response:
        if response.status_code in ok:
            return response
        if response.status_code in {400, 403, 404, 409, 422}:
            raise KeapRejected(call, response.status_code, self._error_code(response))
        raise self._response_error("keap", response, idempotent=idempotent)

    # --- contacts ---------------------------------------------------------

    def get_contact(self, trace: WorkflowTrace, contact_id: str) -> dict[str, Any] | None:
        response = self._call(
            trace, "GET", f"/contacts/{contact_id}", operation="get_contact",
            params={"fields": CONTACT_FIELDS},
        )
        if response.status_code == 404:
            return None  # deleted or merged in Keap
        return self._json_object(
            "keap", self._ok(response, "get contact", idempotent=True),
            operation="get_contact", ambiguous=False,
        )

    def _search(self, trace: WorkflowTrace, filter_: str, operation: str) -> list[dict[str, Any]]:
        response = self._call(
            trace, "GET", "/contacts", operation=operation,
            params={"filter": filter_, "fields": "id,given_name,family_name", "page_size": 10},
        )
        body = self._json_object(
            "keap", self._ok(response, operation, idempotent=True), operation=operation,
            ambiguous=False,
        )
        return list(body.get("contacts") or [])

    def find_by_email(self, trace: WorkflowTrace, email: str) -> list[dict[str, Any]]:
        # Keap's filter language uses ';' (and), ',' (or) and '=='; an address containing them is
        # not a real email and would change the search, so it never matches anyone.
        if not email or any(ch in email for ch in ";,= \t"):
            return []
        return self._search(trace, f"email=={email}", "find_by_email")

    def find_by_phone(self, trace: WorkflowTrace, number: str) -> list[dict[str, Any]]:
        # Keap matches the number exactly as stored. We store E.164, so search with E.164
        # (the sandbox found nothing for bare national digits, "(555) 555-0123" or "555555*").
        return self._search(
            trace, f"phone_number=={number};phone_fields==PHONE1,PHONE2,PHONE3", "find_by_phone"
        )

    def create_contact(self, trace: WorkflowTrace, body: dict[str, Any]) -> dict[str, Any]:
        # No duplicate_option on purpose: the sandbox showed EmailAndName updates a contact
        # that shares the email and family name but has a different first name (a family
        # member). We search by email + full name first, so a plain create is always right.
        # A POST is never retried: a timeout is ambiguous and the next attempt searches first.
        response = self._call(
            trace, "POST", "/contacts", operation="create_contact", json=body, idempotent=False,
        )
        result = self._json_object(
            "keap", self._ok(response, "create contact", idempotent=False, ok=(200, 201)),
            operation="create_contact", ambiguous=True,
        )
        if response.status_code == 201:
            self.stats["created"] += 1
        return result

    def update_contact(
        self, trace: WorkflowTrace, contact_id: str, body: dict[str, Any], mask: list[str]
    ) -> None:
        response = self._call(
            trace, "PATCH", f"/contacts/{contact_id}", operation="update_contact",
            params={"update_mask": ",".join(mask)}, json=body, idempotent=True,
        )
        self._ok(response, "update contact", idempotent=True)
        self.stats["updated"] += 1

    # --- tags -------------------------------------------------------------

    def apply_tag(self, trace: WorkflowTrace, tag_id: str, contact_id: str) -> None:
        response = self._call(
            trace, "POST", f"/tags/{tag_id}/contacts:applyTags", operation="apply_tag",
            json={"contact_ids": [str(contact_id)]}, idempotent=True,
        )
        if response.status_code in {400, 404}:
            raise KeapRejected(f"tag {tag_id}", response.status_code, "not_found")
        self._ok(response, "apply tag", idempotent=True, ok=(200, 204))
        self.stats["tags_applied"] += 1

    def remove_tag(self, trace: WorkflowTrace, tag_id: str, contact_id: str) -> None:
        response = self._call(
            trace, "POST", f"/tags/{tag_id}/contacts:removeTags", operation="remove_tag",
            json={"contact_ids": [str(contact_id)]}, idempotent=True,
        )
        if response.status_code in {400, 404}:
            raise KeapRejected(f"tag {tag_id}", response.status_code, "not_found")
        self._ok(response, "remove tag", idempotent=True, ok=(200, 204))
        self.stats["tags_removed"] += 1

    # --- catalogue (read-only) --------------------------------------------

    def contact_model(self, trace: WorkflowTrace) -> dict[str, Any]:
        response = self._call(trace, "GET", "/contacts/model", operation="contact_model")
        return self._json_object(
            "keap", self._ok(response, "contact model", idempotent=True),
            operation="contact_model", ambiguous=False,
        )

    def field_types(self, trace: WorkflowTrace) -> dict[str, str]:
        """Custom field id -> Keap field type, cached for an hour."""
        if self._model and time.monotonic() - self._model[0] < MODEL_TTL_SECONDS:
            return self._model[1]
        model = self.contact_model(trace)
        types = {str(f["id"]): str(f.get("field_type")) for f in model.get("custom_fields", [])}
        self._model = (time.monotonic(), types)
        return types

    def _paged(self, trace: WorkflowTrace, path: str, key: str, operation: str) -> list[dict]:
        out: list[dict] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": 1000}
            if token:
                params["page_token"] = token
            response = self._call(trace, "GET", path, operation=operation, params=params)
            body = self._json_object(
                "keap", self._ok(response, operation, idempotent=True), operation=operation,
                ambiguous=False,
            )
            out.extend(body.get(key) or [])
            token = body.get("next_page_token")
            if not token:
                return out

    def list_tags(self, trace: WorkflowTrace) -> list[dict[str, Any]]:
        return self._paged(trace, "/tags", "tags", "list_tags")

    def list_tag_categories(self, trace: WorkflowTrace) -> list[dict[str, Any]]:
        return self._paged(trace, "/tags/categories", "tag_categories", "list_tag_categories")


__all__ = [
    "KeapAuthError", "KeapClient", "KeapRateLimited", "KeapRejected", "ProviderError",
    "RateLimiter",
]
