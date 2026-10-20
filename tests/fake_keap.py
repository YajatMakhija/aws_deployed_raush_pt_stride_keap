"""A small in-memory Keap served through httpx.MockTransport.

Implements only the calls the sync makes (docs/PHASE_2B_KEAP_DELIVERY.md section 3), with
the behaviours that matter: PATCH replaces whole arrays, creating with the same email AND
name updates instead of duplicating, applying an existing tag is fine, and failures can
be injected. Automated tests never call the real Keap.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any
from urllib.parse import parse_qs

import httpx

BASE = "/crm/rest/v2"


class FakeKeap:
    def __init__(self):
        self.contacts: dict[str, dict[str, Any]] = {}
        self.tags = {"144": "Pending", "180": "New Evaluation Booked", "172": "Eval Completed",
                     "184": "PT Appt Booked", "176": "PT Appt Completed", "186": "AI Gave Up",
                     "174": "POC Appt Booked", "156": "PT Completed their POC",
                     "178": "Re-Eval Booked", "166": "PT Premium Appt Booked",
                     "164": "PT Premium Appt Completed", "162": "Pelvic Appt Booked - Female",
                     "160": "Pelvic Appt Booked - Male", "158": "Pelvic Appt Completed",
                     "999": "Clinic's own tag"}
        self.custom_fields = {"6": "DATE", "8": "TEXT", "10": "TEXT", "4": "TEXT_AREA"}
        self.requests: list[tuple[str, str]] = []
        self.counts: Counter[str] = Counter()
        self.next_id = 300
        # failure injection
        self.create_queries: list[dict] = []
        self.fail_next: list[int] = []            # status codes returned for the next calls
        self.retry_after: int | None = None       # sent with a 429
        self.unauthorized_once = False
        self.unauthorized_always = False
        self.timeout_on_create = False            # contact IS created, then the response is lost
        self.refresh_rejected = False
        self.missing_contacts: set[str] = set()   # ids that answer 404
        # tokens
        self.access = "access-0"
        self.refresh = "refresh-0"
        self.token_calls = 0
        self.token_issued = 0

    # --- helpers --------------------------------------------------------------

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def add_contact(self, given, family, email=None, phone=None, tags=(), custom=(), **extra):
        cid = str(self.next_id)
        self.next_id += 1
        self.contacts[cid] = {
            "id": cid, "given_name": given, "family_name": family,
            "email_addresses": [{"email": email, "field": "EMAIL1"}] if email else [],
            "phone_numbers": [{"number": phone, "field": "PHONE1"}] if phone else [],
            "birth_date": None, "contact_type": "Customer",
            "custom_fields": [{"id": i, "content": c} for i, c in custom],
            "tag_ids": list(tags), **extra,
        }
        return cid

    def writes(self) -> list[tuple[str, str]]:
        return [r for r in self.requests if r[0] in {"POST", "PATCH"} and "/token" not in r[1]]

    @staticmethod
    def _digits(number):
        d = "".join(c for c in (number or "") if c.isdigit())
        return d[-10:]

    def _json(self, status, body=None, headers=None):
        return httpx.Response(status, json=body if body is not None else {}, headers=headers or {})

    # --- the server ---------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        if path == "/token":
            return self._token(request)
        route = path.removeprefix(BASE)
        self.requests.append((request.method, route))
        self.counts[f"{request.method} {route.split('?')[0]}"] += 1

        auth = request.headers.get("authorization", "")
        if self.unauthorized_always:
            return self._json(401, {"code": 401, "status": "UNAUTHENTICATED"})
        if self.unauthorized_once:
            self.unauthorized_once = False
            return self._json(401, {"code": 401, "status": "UNAUTHENTICATED"})
        if auth != f"Bearer {self.access}":  # a stale token is refused like the real thing
            return self._json(401, {"code": 401, "status": "UNAUTHENTICATED"})
        if self.fail_next:
            code = self.fail_next.pop(0)
            headers = {"Retry-After": str(self.retry_after)} if code == 429 and self.retry_after else {}
            return self._json(code, {"code": code, "status": "ERROR"}, headers)

        body = json.loads(request.content) if request.content else None
        if route == "/businessProfile":
            return self._json(200, {"business_name": "sandbox"}, {"x-keap-tenant-id": "fake.infusionsoft.com"})
        if route == "/contacts/model":
            return self._json(200, {"custom_fields": [
                {"id": i, "label": f"Field {i}", "field_type": t} for i, t in self.custom_fields.items()]})
        if route == "/tags":
            return self._json(200, {"tags": [{"id": i, "name": n} for i, n in self.tags.items()]})
        if route == "/tags/categories":
            return self._json(200, {"tag_categories": []})
        if route == "/contacts":
            if request.method == "GET":
                return self._search(query)
            return self._create(query, body)
        if route.startswith("/contacts/") and ":" not in route:
            cid = route.split("/")[2]
            if cid in self.missing_contacts or cid not in self.contacts:
                return self._json(404, {"code": 404, "status": "NOT_FOUND"})
            if request.method == "GET":
                return self._json(200, self.contacts[cid])
            return self._patch(cid, query, body)
        if route.startswith("/tags/") and (route.endswith(":applyTags") or route.endswith(":removeTags")):  # noqa: PIE810
            tag = route.split("/")[2]
            if tag not in self.tags:
                return self._json(404, {"code": 404, "status": "NOT_FOUND"})
            cids = [str(c) for c in body["contact_ids"]]
            for cid in cids:
                tags = self.contacts[cid]["tag_ids"]
                if route.endswith(":applyTags") and tag not in tags:
                    tags.append(tag)
                if route.endswith(":removeTags") and tag in tags:
                    tags.remove(tag)
            if route.endswith(":removeTags"):
                return httpx.Response(204)
            return self._json(200, {"results": {c: "SUCCESS" for c in cids}})
        return self._json(404, {"code": 404, "status": "NOT_FOUND"})

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.token_calls += 1
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form.get("grant_type") == "refresh_token":
            if self.refresh_rejected or form.get("refresh_token") != self.refresh:
                return httpx.Response(400, json={"error": "invalid_grant"})
        elif form.get("grant_type") != "authorization_code" or form.get("code") != "good-code":
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.token_issued += 1
        self.access, self.refresh = f"access-{self.token_issued}", f"refresh-{self.token_issued}"
        return httpx.Response(200, json={
            "access_token": self.access, "refresh_token": self.refresh,
            "expires_in": 86400, "token_type": "bearer", "scope": "full"})

    def _search(self, query):
        filter_ = query.get("filter", "")
        parts = dict(p.split("==", 1) for p in filter_.split(";") if "==" in p)
        found = []
        for c in self.contacts.values():
            if "email" in parts and any(
                    (e["email"] or "").lower() == parts["email"].lower() for e in c["email_addresses"]) or "phone_number" in parts and any(
                    self._digits(p["number"]) == self._digits(parts["phone_number"])
                    for p in c["phone_numbers"]):
                found.append(c)
        return self._json(200, {"contacts": [
            {"id": c["id"], "given_name": c["given_name"], "family_name": c["family_name"]}
            for c in found]})

    def _validate_custom(self, fields):
        for f in fields or []:
            if str(f["id"]) not in self.custom_fields:
                return self._json(400, {"code": 400, "status": "INVALID_ARGUMENT"})
        return None

    def _create(self, query, body):
        self.create_queries.append(dict(query))
        if not (body.get("email_addresses") or body.get("phone_numbers") or body.get("addresses")):
            return self._json(400, {"code": 400, "status": "INVALID_ARGUMENT"})
        bad = self._validate_custom(body.get("custom_fields"))
        if bad:
            return bad
        if query.get("duplicate_option") == "EmailAndName" and body.get("email_addresses"):
            email = body["email_addresses"][0]["email"].lower()
            for c in self.contacts.values():
                if (any((e["email"] or "").lower() == email for e in c["email_addresses"])
                        and c["given_name"] == body.get("given_name")
                        and c["family_name"] == body.get("family_name")):
                    c.update({k: v for k, v in body.items()})
                    return self._json(200, c)
        cid = self.add_contact(body.get("given_name"), body.get("family_name"))
        contact = self.contacts[cid]
        contact.update({k: v for k, v in body.items() if k in contact})
        if self.timeout_on_create:
            self.timeout_on_create = False
            raise httpx.ReadTimeout("response lost")
        return self._json(201, contact)

    def _patch(self, cid, query, body):
        bad = self._validate_custom(body.get("custom_fields"))
        if bad:
            return bad
        mask = [m for m in query.get("update_mask", "").split(",") if m]
        for key in mask or list(body):
            if key not in body:
                continue
            if key == "custom_fields":
                # Observed in the sandbox: merged by id, and null content clears a field.
                held = {f["id"]: f for f in self.contacts[cid]["custom_fields"]}
                for f in body[key]:
                    held[str(f["id"])] = {"id": str(f["id"]), "content": f["content"]}
                self.contacts[cid][key] = list(held.values())
            else:
                self.contacts[cid][key] = body[key]  # other arrays are replaced whole
        return self._json(200, self.contacts[cid])
