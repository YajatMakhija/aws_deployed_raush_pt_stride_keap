"""Keap sync, phase 2B: make one person's Keap contact match `keap_sync.desired`.

Every Keap write below is safe to repeat, and the steps run in a fixed order:
find (or create) the contact, patch only the fields we own, apply wanted tags first,
then remove owned tags that are no longer wanted (so a person is never left without a
status tag). What Keap holds that we do not own - other emails and phones, other
custom fields, other tags, the contact type of an existing contact - is left alone.
Never log names, emails, phones or birthdays; `last_error` holds short PHI-free reasons.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from ..config import Settings, get_settings
from ..db import transaction
from ..observability import WorkflowTrace
from ..retry import retry_delay_seconds
from .keap_client import (
    KeapAuthError,
    KeapClient,
    KeapRateLimited,
    KeapRejected,
)
from .provider_http import ProviderError

TEXT_TYPES = {"TEXT", "TEXT_AREA"}


class UnsupportedField(RuntimeError):
    pass


# --- value formatting --------------------------------------------------------


def format_field(field_id: str, kind: str, value: Any, field_types: dict[str, str]) -> str:
    """The content string Keap expects for one of our owned custom fields."""
    field_type = field_types.get(str(field_id))
    if field_type is None:
        raise UnsupportedField(f"field {field_id} not found in Keap")
    if kind == "date" and field_type == "DATE":
        return str(value)  # YYYY-MM-DD; confirmed against the sandbox
    if kind in {"time", "text"} and field_type in TEXT_TYPES:
        return str(value)
    if kind == "date" and field_type in TEXT_TYPES:
        return str(value)
    raise UnsupportedField(f"unsupported field type {field_type} for field {field_id}")


def desired_custom_fields(desired: dict[str, Any], field_types: dict[str, str]) -> dict[str, str]:
    """Owned fields with a value, formatted. A null value is absent (it clears the field)."""
    return {
        field_id: format_field(field_id, spec["kind"], spec["value"], field_types)
        for field_id, spec in desired["fields"].items()
        if spec["value"] is not None
    }


def _digits(number: str | None) -> str:
    digits = "".join(c for c in (number or "") if c.isdigit())
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def _norm(content: Any) -> str:
    return str(content)[:10] if content is not None and len(str(content)) >= 10 and str(
        content)[4:5] == "-" else str(content or "")


# --- building the Keap request ----------------------------------------------


def create_body(desired: dict[str, Any], custom: dict[str, str]) -> dict[str, Any]:
    c = desired["contact"]
    body: dict[str, Any] = {
        "given_name": c["given_name"],
        "family_name": c["family_name"],
        "contact_type": c["contact_type"],
    }
    if c.get("birth_date"):
        body["birth_date"] = c["birth_date"]
    if c.get("email"):
        body["email_addresses"] = [{"email": c["email"], "field": "EMAIL1"}]
    if c.get("phone"):
        body["phone_numbers"] = [{"number": c["phone"], "field": "PHONE1"}]
    if c.get("address"):
        body["addresses"] = [{"field": "BILLING", **{k: v for k, v in c["address"].items() if v}}]
    if custom:
        body["custom_fields"] = [{"id": i, "content": v} for i, v in sorted(custom.items())]
    return body


def update_body(
    contact: dict[str, Any], desired: dict[str, Any], custom: dict[str, str]
) -> tuple[dict[str, Any], list[str]]:
    """Only what differs; arrays are rebuilt from what Keap holds because Keap replaces them whole."""
    c = desired["contact"]
    body: dict[str, Any] = {}
    for key in ("given_name", "family_name"):
        if c.get(key) and contact.get(key) != c[key]:
            body[key] = c[key]
    if c.get("birth_date") and _norm(contact.get("birth_date")) != c["birth_date"]:
        body["birth_date"] = c["birth_date"]

    emails = list(contact.get("email_addresses") or [])
    if c.get("email") and not any(
        (e.get("email") or "").casefold() == c["email"].casefold() for e in emails
    ):
        slot1 = next((e for e in emails if e.get("field") == "EMAIL1"), None)
        if slot1:
            emails = [{**e, "email": c["email"]} if e is slot1 else e for e in emails]
        else:
            emails.append({"email": c["email"], "field": "EMAIL1"})
        body["email_addresses"] = emails

    phones = list(contact.get("phone_numbers") or [])
    if c.get("phone") and not any(_digits(p.get("number")) == _digits(c["phone"]) for p in phones):
        slot1 = next((p for p in phones if p.get("field") == "PHONE1"), None)
        if slot1:
            phones = [{**p, "number": c["phone"]} if p is slot1 else p for p in phones]
        else:
            phones.append({"number": c["phone"], "field": "PHONE1"})
        body["phone_numbers"] = phones

    # Custom fields: send only our own, and only those that differ. The sandbox merges
    # custom_fields by id (a PATCH with one field left the others alone) and clears a
    # field when its content is null, so a cleared date is {"id": .., "content": null}.
    # Fields the clinic owns are never sent, so they cannot be disturbed.
    held = {
        str(f["id"]): f.get("content")
        for f in (contact.get("custom_fields") or [])
        if str(f["id"]) in {str(i) for i in desired["owned_fields"]}
    }
    changes = []
    for field_id in sorted(desired["owned_fields"]):
        want = custom.get(field_id)
        if _norm(held.get(field_id)) != _norm(want):
            changes.append({"id": field_id, "content": want})
    if changes:
        body["custom_fields"] = changes

    if c.get("address") and not contact.get("addresses"):
        body["addresses"] = [{"field": "BILLING", **{k: v for k, v in c["address"].items() if v}}]
    return body, sorted(body)


# --- one person --------------------------------------------------------------


def _save_contact_id(row_id: int, contact_id: str | None) -> None:
    with transaction() as conn:
        conn.execute("update keap_sync set keap_contact_id=%s where id=%s", (contact_id, row_id))


def _find_existing(client: KeapClient, trace: WorkflowTrace, match: dict[str, Any]) -> list[str]:
    """Contacts that are this person: same name AND a shared email (else a shared phone)."""
    given = (match.get("given_name") or "").strip().casefold()
    family = (match.get("family_name") or "").strip().casefold()

    def same_person(candidates: list[dict[str, Any]]) -> list[str]:
        return [
            str(x["id"]) for x in candidates
            if (x.get("given_name") or "").strip().casefold() == given
            and (x.get("family_name") or "").strip().casefold() == family
        ]

    if match.get("email"):
        found = same_person(client.find_by_email(trace, match["email"]))
        if found:
            return found
    if match.get("phone"):
        return same_person(client.find_by_phone(trace, match["phone"]))
    return []


def sync_one(client: KeapClient, row: dict[str, Any], trace: WorkflowTrace) -> str:
    """Returns 'synced', 'requeued' (2A changed it meanwhile) or 'needs_review'."""
    desired = row["desired"]
    field_types = client.field_types(trace)
    custom = desired_custom_fields(desired, field_types)

    contact: dict[str, Any] | None = None
    contact_id = row["keap_contact_id"]
    if contact_id:
        contact = client.get_contact(trace, contact_id)
        if contact is None:  # deleted or merged in Keap
            _save_contact_id(row["id"], None)
            contact_id = None
    if not contact_id:
        found = _find_existing(client, trace, desired["match"])
        if len(found) > 1:
            with transaction() as conn:
                conn.execute(
                    "update keap_sync set status='needs_review',last_error=%s where id=%s",
                    ("several Keap contacts match", row["id"]),
                )
            return "needs_review"
        if found:
            contact_id = found[0]
            with transaction() as conn:
                taken = conn.execute(
                    "select 1 from keap_sync where practice_id=%s and keap_contact_id=%s and id<>%s",
                    (row["practice_id"], contact_id, row["id"]),
                ).fetchone()
                if taken:
                    # Two people resolving to one contact would fight over its tags.
                    conn.execute(
                        "update keap_sync set status='needs_review',last_error=%s where id=%s",
                        ("Keap contact already belongs to another person", row["id"]),
                    )
                    return "needs_review"
        else:
            created = client.create_contact(trace, create_body(desired, custom))
            contact_id = str(created["id"])
        # Saved at once, in its own transaction: a crash after the create must not lose
        # the id, or the next attempt could create a second contact.
        _save_contact_id(row["id"], contact_id)
        contact = client.get_contact(trace, contact_id)
        if contact is None:
            raise ProviderError("keap", "contact_missing", "contact not readable after create",
                                retryable=True)

    body, mask = update_body(contact, desired, custom)
    if body:
        client.update_contact(trace, contact_id, body, mask)

    have = {str(t) for t in (contact.get("tag_ids") or [])}
    want = {str(t) for t in desired["tags"]}
    owned = {str(t) for t in desired["owned_tags"]}
    for tag_id in sorted(want - have):  # apply first: never leave a person without a status tag
        client.apply_tag(trace, tag_id, contact_id)
    for tag_id in sorted((owned & have) - want):
        client.remove_tag(trace, tag_id, contact_id)

    with transaction() as conn:
        done = conn.execute(
            "update keap_sync set status='synced',synced=%s,synced_hash=%s,synced_at=now(),"
            "attempts=0,last_error=null where id=%s and desired_hash=%s returning id",
            (Jsonb(desired), row["desired_hash"], row["id"], row["desired_hash"]),
        ).fetchone()
        if done is None:  # 2A changed the plan while we worked: do it again, not 'synced'
            conn.execute(
                "update keap_sync set status='pending',next_attempt_at=now() where id=%s",
                (row["id"],),
            )
            return "requeued"
    return "synced"


# --- failures ----------------------------------------------------------------


def _update(row_id: int, assignments: str, *params: Any) -> None:
    with transaction() as conn:
        conn.execute(f"update keap_sync set {assignments} where id=%s", (*params, row_id))


def record_failure(row: dict[str, Any], error: Exception, settings: Settings) -> str:
    """Store what happened to one row. Returns the new status."""
    attempts = int(row["attempts"])
    if isinstance(error, KeapAuthError):
        # Not the row's fault: it waits for a reconnect without using an attempt.
        _update(row["id"], "status='pending',attempts=greatest(attempts-1,0),last_error=%s",
                "Keap connection needs attention")
        return "pending"
    if isinstance(error, KeapRateLimited):
        _update(row["id"],
                "status='retrying',attempts=greatest(attempts-1,0),last_error=%s,"
                "next_attempt_at=now()+make_interval(secs=>%s)",
                "Keap rate limit; waiting", int(error.retry_after_seconds))
        return "retrying"
    if isinstance(error, UnsupportedField):
        _update(row["id"], "status='failed',last_error=%s", str(error)[:200])
        return "failed"
    if isinstance(error, KeapRejected):
        reason = (
            f"{error.call} not found in Keap" if error.call.startswith("tag ") else str(error)
        )
        _update(row["id"], "status='failed',last_error=%s", reason[:200])
        return "failed"
    if isinstance(error, ProviderError):
        reason, retry_after = f"Keap error {error.code}", error.retry_after_seconds
    else:
        reason, retry_after = f"unexpected error {type(error).__name__}", None
    if attempts >= settings.keap_sync_max_attempts:
        _update(row["id"], "status='failed',last_error=%s", reason)
        return "failed"
    delay = retry_delay_seconds(attempts, retry_after, settings)
    _update(row["id"], "status='retrying',last_error=%s,next_attempt_at=now()+make_interval(secs=>%s)",
            reason, int(delay))
    return "retrying"


def sync_row(client: KeapClient, row: dict[str, Any], trace: WorkflowTrace,
             settings: Settings | None = None) -> str:
    """sync_one plus failure bookkeeping. KeapAuthError / KeapRateLimited are re-raised
    after the row is parked, so the caller can stop the whole tick."""
    settings = settings or get_settings()
    try:
        return sync_one(client, row, trace)
    except (KeapAuthError, KeapRateLimited) as error:
        record_failure(row, error, settings)
        raise
    except Exception as error:  # noqa: BLE001 - one person must not stop the rest
        trace.log("keap_sync_row_failed", row_id=row["id"], error_category=type(error).__name__)
        return record_failure(row, error, settings)
