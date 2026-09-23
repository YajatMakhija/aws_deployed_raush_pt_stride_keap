from __future__ import annotations

import json
import re
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from ..db import transaction
from ..observability import WorkflowTrace
from ..providers import ProviderClients, ProviderError
from ..security import sign_slot, verify_slot
from .lead_status import mark_booked
from .review import flag_lead_for_review


class BookingService:
    def __init__(self, providers: ProviderClients | None = None):
        self.providers = providers or ProviderClients()

    @staticmethod
    def _parse_time(value: str) -> clock_time:
        text = re.sub(r"\s+", " ", value.strip().upper())
        for pattern in ("%I %p", "%I:%M %p", "%H:%M", "%H:%M:%S"):
            try:
                return datetime.strptime(text, pattern).replace(tzinfo=UTC).time()
            except ValueError:
                continue
        raise ValueError("Please say the time like 9 AM, 2:30 PM, or 14:30.")

    @staticmethod
    def _spoken_time(value: str) -> str:
        return clock_time.fromisoformat(value).strftime("%I:%M %p").lstrip("0")

    @staticmethod
    def _slot_token(lead_id: str, slot, expires: int) -> str:
        payload = json.dumps(
            {
                "lead_id": lead_id,
                "clinician_id": slot.clinician_id,
                "date": slot.local_date,
                "time": slot.local_time,
                "timezone": slot.timezone,
            },
            separators=(",", ":"),
        )
        return sign_slot(payload, expires)

    @staticmethod
    def _booking_context(lead_id: str | None, practice_id: int | None = None) -> dict[str, Any]:
        with transaction() as conn:
            if lead_id:
                row = conn.execute(
                    "select l.id,ps.*,p.timezone from leads l join practice_settings ps "
                    "on ps.practice_id=l.practice_id join practices p on p.id=l.practice_id "
                    "where l.id=%s",
                    (lead_id,),
                ).fetchone()
            elif practice_id is not None:
                row = conn.execute(
                    "select null::uuid as id,ps.*,p.timezone from practice_settings ps "
                    "join practices p on p.id=ps.practice_id where ps.practice_id=%s",
                    (practice_id,),
                ).fetchone()
            else:
                raise ValueError(
                    "I could not load this patient's record. Would you like a staff member to follow up?"
                )
        if not row:
            raise ValueError(
            "I could not load this patient's record. Would you like a staff member to follow up?"
        )
        return row

    def availability(
        self,
        trace: WorkflowTrace,
        lead_id: str | None,
        start: date | None,
        days: int = 14,
        *,
        practice_id: int | None = None,
        limit: int | None = 10,
    ) -> dict[str, Any]:
        row = self._booking_context(lead_id, practice_id)
        start = start or datetime.now(ZoneInfo(row["stride_location_timezone"])).date()
        trace.log("request_parsed", lead_id=lead_id, start_date=start.isoformat(), days=days)
        slots = self.providers.stride_availability(
            trace,
            location=row["stride_location_id"],
            duration=row["stride_default_duration_mins"],
            clinician_ids=row["stride_clinician_ids"],
            start_date=start,
            end_date=min(start + timedelta(days=max(1, days) - 1), start + timedelta(days=30)),
        )
        slots.sort(key=lambda item: (item.local_date, item.local_time, item.clinician_id))
        seen: set[tuple[str, str]] = set()
        # One offer per wall-clock slot; several clinicians share the same time.
        slots = [
            slot for slot in slots
            if not ((slot.local_date, slot.local_time) in seen
                    or seen.add((slot.local_date, slot.local_time)))
        ]
        if limit is not None:
            slots = slots[:limit]
        expires = int(time.time()) + 300
        values = []
        for slot in slots:
            display_time = self._spoken_time(slot.local_time)
            values.append({
                "date": slot.local_date,
                "time": slot.local_time,
                "spoken_time": display_time,
                "timezone": slot.timezone,
                "slot_token": self._slot_token(str(lead_id or ""), slot, expires),
            })
        trace.log("availability_prepared", slot_count=len(values))
        return {"status": "ok", "slots": values}

    def availability_message(
        self,
        trace: WorkflowTrace,
        *,
        lead_id: str | None,
        practice_id: int | None,
        requested_date: date | None,
        requested_time: str | None,
    ) -> str:
        parsed_time = self._parse_time(requested_time) if requested_time else None
        result = self.availability(
            trace,
            lead_id,
            requested_date,
            14,
            practice_id=practice_id,
            limit=None,
        )
        slots = result["slots"]
        if requested_date:
            same_day = [slot for slot in slots if slot["date"] == requested_date.isoformat()]
            if parsed_time:
                normalized = parsed_time.strftime("%H:%M:%S")
                if any(slot["time"] == normalized for slot in same_day):
                    return (
                        f"Yes, {parsed_time.strftime('%I:%M %p').lstrip('0')} on "
                        f"{requested_date.isoformat()} is open. Shall I book it?"
                    )
                same_day.sort(
                    key=lambda slot: abs(
                        clock_time.fromisoformat(slot["time"]).hour * 60
                        + clock_time.fromisoformat(slot["time"]).minute
                        - (parsed_time.hour * 60 + parsed_time.minute)
                    )
                )
            if same_day:
                # Nearest-first picks the offers; speak them in clock order.
                nearest = sorted(same_day[:2], key=lambda slot: slot["time"])
                offered = " and ".join(slot["spoken_time"] for slot in nearest)
                return (
                    f"The closest openings on {requested_date.isoformat()} are {offered}. "
                    "Which works better?"
                    if parsed_time
                    else f"I have {offered} open on {requested_date.isoformat()}. Which works better?"
                )
            later = slots[:2]
            if later:
                offered = " and ".join(
                    f"{slot['spoken_time']} on {slot['date']}" for slot in later
                )
                return (
                    f"Nothing is open on {requested_date.isoformat()}. The next openings are "
                    f"{offered}. Would either work?"
                )
            return (
                f"Nothing is open from {requested_date.isoformat()} through the next two weeks. "
                "Would you like a staff member to look further out?"
            )
        if slots:
            offered = " and ".join(
                f"{slot['spoken_time']} on {slot['date']}" for slot in slots[:2]
            )
            return f"The next openings are {offered}. Which works better?"
        return "I could not find an opening in the next two weeks. Would you like staff to look further out?"

    def book_at(
        self,
        trace: WorkflowTrace,
        *,
        lead_id: str,
        event_id: int | None,
        appointment_date: date,
        appointment_time: str,
        patient_data: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        with transaction() as conn:
            existing = conn.execute(
                "select state,stride_appointment_id from appointments where lead_id=%s "
                "and state in ('booking','scheduled','unknown') order by id desc limit 1",
                (lead_id,),
            ).fetchone()
        if existing:
            if existing["state"] == "booking":
                return {"status": "booking_in_progress"}
            if existing["state"] == "unknown":
                return {"status": "manual_review"}
            return {
                "status": "already_booked",
                "appointment_id": existing["stride_appointment_id"],
            }
        parsed_time = self._parse_time(appointment_time)
        context = self._booking_context(lead_id)
        if not context["stride_booking_enabled"]:
            return {"status": "configuration_required"}
        slots = self.providers.stride_availability(
            trace,
            location=context["stride_location_id"],
            duration=context["stride_default_duration_mins"],
            clinician_ids=context["stride_clinician_ids"],
            start_date=appointment_date,
            end_date=appointment_date,
        )
        normalized = parsed_time.strftime("%H:%M:%S")
        slot = next(
            (
                item for item in slots
                if item.local_date == appointment_date.isoformat() and item.local_time == normalized
            ),
            None,
        )
        if slot is None:
            return {"status": "slot_unavailable"}
        token = self._slot_token(lead_id, slot, int(time.time()) + 300)
        return self.book(trace, lead_id, event_id, token, patient_data)

    def book(
        self,
        trace: WorkflowTrace,
        lead_id: str,
        event_id: int | None,
        slot_token: str,
        patient_data: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        trace.log("request_parsed", lead_id=lead_id, event_id=event_id)
        payload_text, _ = verify_slot(slot_token)
        slot = json.loads(payload_text)
        if slot.get("lead_id") != lead_id:
            raise ValueError("slot token belongs to another lead")
        with transaction() as conn:
            lead = conn.execute(
                "select l.*,ps.stride_location_id,ps.stride_appointment_type_id,"
                "ps.stride_default_duration_mins,ps.stride_case_title,ps.stride_location_timezone,"
                "ps.stride_booking_enabled "
                "from leads l join practice_settings ps on ps.practice_id=l.practice_id "
                "where l.id=%s for update of l",
                (lead_id,),
            ).fetchone()
            if not lead:
                raise ValueError("lead not found")
            patient_data = patient_data or {}
            first_name = lead["first_name"] or patient_data.get("first_name", "").strip()
            last_name = lead["last_name"] or patient_data.get("last_name", "").strip()
            date_of_birth = lead["date_of_birth"]
            if not date_of_birth and patient_data.get("date_of_birth"):
                date_of_birth = date.fromisoformat(patient_data["date_of_birth"])
            if not first_name or not last_name or not date_of_birth:
                return {"status": "missing_patient_data"}
            if (
                first_name != lead["first_name"]
                or last_name != lead["last_name"]
                or date_of_birth != lead["date_of_birth"]
            ):
                conn.execute(
                    "update leads set first_name=%s,last_name=%s,date_of_birth=%s,"
                    "full_name=trim(%s || ' ' || %s) where id=%s",
                    (first_name, last_name, date_of_birth, first_name, last_name, lead_id),
                )
                lead["first_name"] = first_name
                lead["last_name"] = last_name
                lead["date_of_birth"] = date_of_birth
            if event_id is not None:
                event = conn.execute(
                    "select lead_id,channel from outreach_events where id=%s", (event_id,)
                ).fetchone()
                if not event or str(event["lead_id"]) != lead_id or event["channel"] != "call":
                    raise ValueError("lead and call outreach event do not match")
            existing = conn.execute(
                "select id,state,stride_appointment_id from appointments where lead_id=%s "
                "and state in ('booking','scheduled','unknown') order by id desc limit 1", (lead_id,),
            ).fetchone()
            if existing:
                if existing["state"] == "booking":
                    return {"status": "booking_in_progress"}
                if existing["state"] == "unknown":
                    return {"status": "manual_review"}
                return {
                    "status": "already_booked",
                    "appointment_id": existing["stride_appointment_id"],
                }
            if not lead["stride_booking_enabled"]:
                return {"status": "configuration_required"}
            start_utc, end_utc = self._slot_utc(slot, lead["stride_default_duration_mins"])
            booking_key = f"{lead_id}:{start_utc.isoformat()}"
            appointment = conn.execute(
                "select id from appointments where booking_key=%s and state='failed' for update",
                (booking_key,),
            ).fetchone()
            if appointment:
                conn.execute(
                    "update appointments set state='booking',outreach_event_id=%s,stride_error=null,"
                    "needs_staff_review=false,updated_at=now() where id=%s",
                    (event_id, appointment["id"]),
                )
            else:
                appointment = conn.execute(
                    "insert into appointments(lead_id,practice_id,outreach_event_id,booking_source,state,"
                    "booking_key,clinician_id,location_id,appointment_type_id) "
                    "values(%s,%s,%s,'voice_agent','booking',%s,%s,%s,%s) "
                    "on conflict do nothing returning id",
                    (
                        lead_id,
                        lead["practice_id"],
                        event_id,
                        booking_key,
                        slot["clinician_id"],
                        lead["stride_location_id"],
                        lead["stride_appointment_type_id"],
                    ),
                ).fetchone()
                if not appointment:
                    existing = conn.execute(
                        "select id,state,stride_appointment_id from appointments where lead_id=%s "
                        "and state in ('booking','scheduled','unknown') order by id desc limit 1",
                        (lead_id,),
                    ).fetchone()
                    if not existing or existing["state"] == "booking":
                        return {"status": "booking_in_progress"}
                    if existing["state"] == "unknown":
                        return {"status": "manual_review"}
                    return {
                        "status": "already_booked",
                        "appointment_id": existing["stride_appointment_id"],
                    }
            local_id = appointment["id"]
            trace.log("booking_reserved", appointment_id=local_id)

        stage = "availability"
        try:
            live = self.providers.stride_availability(
                trace,
                location=lead["stride_location_id"],
                duration=lead["stride_default_duration_mins"],
                clinician_ids=str(slot["clinician_id"]),
                start_date=date.fromisoformat(slot["date"]),
                end_date=date.fromisoformat(slot["date"]),
            )
            if not any(
                item.local_date == slot["date"] and item.local_time == slot["time"] for item in live
            ):
                self._mark_booking(local_id, "failed", "slot unavailable")
                return {"status": "slot_unavailable"}
            patient_id = lead["stride_patient_id"]
            if not patient_id:
                stage = "patient"
                patient_id = self.providers.stride_create(trace, "patients", {
                    "first_name": lead["first_name"],
                    "last_name": lead["last_name"],
                    "date_of_birth": lead["date_of_birth"].isoformat(),
                    "contact_info": {
                        "mobile_phone_number": lead["phone_e164"],
                        "personal_email": lead["email"] or "",
                        "preferred_contact_method": "P",
                    },
                    "primary_address": {},
                })
                with transaction() as conn:
                    conn.execute(
                        "update leads set stride_patient_id=%s where id=%s and stride_patient_id is null",
                        (patient_id, lead_id),
                    )
                lead["stride_patient_id"] = patient_id
            case_id = lead["stride_case_id"]
            if not case_id:
                stage = "case"
                case_id = self.providers.stride_create(
                    trace, "cases", {"patient_id": patient_id, "title": lead["stride_case_title"]}
                )
                with transaction() as conn:
                    conn.execute(
                        "update leads set stride_case_id=%s where id=%s and stride_case_id is null",
                        (case_id, lead_id),
                    )
                lead["stride_case_id"] = case_id
            stage = "appointment"
            stride_id = self.providers.stride_create(trace, "appointments", {
                "case_id": case_id,
                "primary_attendee": slot["clinician_id"],
                "location": lead["stride_location_id"],
                "appointment_type": lead["stride_appointment_type_id"],
                "start_date_utc": start_utc.isoformat(),
                "end_date_utc": end_utc.isoformat(),
                "is_pending": True,
                "appointment_status": "O",
            })
        except ProviderError as exc:
            state = "unknown" if exc.ambiguous else "failed"
            self._mark_booking(local_id, state, str(exc))
            detail = str(exc).lower()
            if exc.code == "400" and stage == "patient" and "already exists" in detail:
                self._flag_review(lead_id, "Stride duplicate patient requires mapping")
                return {"status": "manual_review"}
            if exc.code == "400" and stage == "appointment" and "overlapping" in detail:
                return {"status": "slot_unavailable"}
            if exc.code == "400" and stage == "appointment" and "already exists" in detail:
                self._flag_review(lead_id, "Stride reports an existing appointment; reconcile mapping")
                return {"status": "manual_review"}
            if exc.ambiguous:
                self._flag_review(lead_id, "ambiguous Stride booking result; do not retry")
                return {"status": "manual_review"}
            if exc.retryable:
                return {"status": "retry_later"}
            return {"status": "failed"}
        except Exception as exc:  # noqa: BLE001 - external progress may have occurred
            self._mark_booking(local_id, "unknown", f"unexpected booking error: {type(exc).__name__}")
            self._flag_review(lead_id, "unexpected Stride booking result; reconcile before retry")
            return {"status": "manual_review"}

        try:
            with transaction() as conn:
                conn.execute(
                    "update appointments set state='scheduled',stride_appointment_id=%s,start_utc=%s,"
                    "end_utc=%s,confirmed_at=now(),updated_at=now() where id=%s",
                    (stride_id, start_utc, end_utc, local_id),
                )
        except Exception as exc:  # noqa: BLE001 - Stride is the booking source of truth
            trace.log(
                "booking_local_sync_failed",
                appointment_id=local_id,
                stride_appointment_id=stride_id,
                error_category=type(exc).__name__,
            )
            return {
                "status": "confirmed_sync_pending",
                "appointment_id": stride_id,
                "spoken_confirmation": "Your appointment is confirmed. Our staff will verify the details.",
            }

        try:
            with transaction() as conn:
                conn.execute(
                    "update leads set stride_patient_id=%s,stride_case_id=%s where id=%s",
                    (patient_id, case_id, lead_id),
                )
                if event_id is not None:
                    conn.execute(
                        "update outreach_events set status='delivered',settled_at=now(),settled_by='tool',"
                        "outcome='booked' where id=%s and lead_id=%s "
                        "and status not in ('delivered','failed','skipped')",
                        (event_id, lead_id),
                    )
                mark_booked(conn, lead_id, "stride_booking")
                conn.execute(
                    "insert into notification_log(lead_id,appointment_id,notification_type,channel,status,payload) "
                    "values(%s,%s,'sms_appointment_booked','sms','queued',%s) on conflict do nothing",
                    (lead_id, local_id, json.dumps({"start_utc": start_utc.isoformat()})),
                )
                event_payload = {
                    "event_type": "appointment.booked.v1",
                    "event_id": str(uuid4()),
                    "lead_id": lead_id,
                    "first_name": lead["first_name"],
                    "last_name": lead["last_name"],
                    "email": lead["email"],
                    "phone": lead["phone_e164"],
                    "birthday": lead["date_of_birth"].isoformat(),
                    "appointment_type_id": lead["stride_appointment_type_id"],
                    "appointment_start_utc": start_utc.isoformat(),
                    "provider_id": slot["clinician_id"],
                    "stride_appointment_id": stride_id,
                }
                conn.execute(
                    "insert into integration_outbox(event_id,event_type,aggregate_id,payload,status,destination) "
                    "values(%s,'appointment.booked.v1',%s,%s,'pending','keap') "
                    "on conflict(event_id) do nothing",
                    (event_payload["event_id"], str(local_id), json.dumps(event_payload)),
                )
        except Exception as exc:  # noqa: BLE001 - Stride is the booking source of truth
            trace.log(
                "booking_local_sync_failed",
                appointment_id=local_id,
                stride_appointment_id=stride_id,
                error_category=type(exc).__name__,
            )
            return {
                "status": "confirmed_sync_pending",
                "appointment_id": stride_id,
                "spoken_confirmation": "Your appointment is confirmed. Our staff will verify the details.",
            }
        trace.log("booking_confirmed", appointment_id=local_id, stride_appointment_id=stride_id)
        local_display = datetime.fromisoformat(f"{slot['date']}T{slot['time']}").strftime(
            "%A, %B %d at %I:%M %p"
        ).replace(" 0", " ")
        return {
            "status": "confirmed",
            "appointment_id": stride_id,
            "spoken_confirmation": f"Your appointment is confirmed for {local_display}.",
        }

    @staticmethod
    def _slot_utc(slot: dict[str, Any], duration: int) -> tuple[datetime, datetime]:
        local = datetime.fromisoformat(f"{slot['date']}T{slot['time']}").replace(
            tzinfo=ZoneInfo(slot["timezone"])
        )
        start = local.astimezone(UTC)
        return start, start + timedelta(minutes=duration)

    @staticmethod
    def _mark_booking(appointment_id: int, state: str, error: str) -> None:
        with transaction() as conn:
            conn.execute(
                "update appointments set state=%s,stride_error=%s,needs_staff_review=%s,"
                "updated_at=now() where id=%s",
                (state, error[:500], state == "unknown", appointment_id),
            )

    @staticmethod
    def _flag_review(lead_id: str, reason: str) -> None:
        with transaction() as conn:
            flag_lead_for_review(conn, lead_id, reason)
