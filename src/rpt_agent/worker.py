from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from uuid import uuid4
from zoneinfo import ZoneInfo

from .config import get_settings
from .db import transaction
from .observability import WorkflowTrace, configure_logging
from .providers import ProviderClients, ProviderError
from .retry import retry_delay_seconds
from .services.delivery import (
    process_pending_integrations,
    reprocess_failed_twilio_events,
    reprocess_failed_vapi_events,
)
from .usage_report import record_test_usage


@dataclass(frozen=True)
class Job:
    event_id: int
    lead_id: str
    channel: str
    phone: str
    name: str
    body: str | None
    booking_link_url: str | None
    day_offset: int | None
    vapi_assistant_id: str | None
    vapi_phone_number_id: str | None
    attempt_no: int = 1
    date_of_birth: date | None = None
    case_title: str = ""
    location_id: int | None = None


@dataclass(frozen=True)
class DispatchResult:
    job: Job
    state: str
    value: str
    retry_after_seconds: int | None = None


def format_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = re.sub(r"[^\d+]", "", str(raw).strip())
    digits = cleaned.replace("+", "")
    if len(digits) < 10 or len(digits) > 15:
        return None
    if cleaned.startswith("+"):
        return cleaned if len(digits) >= 11 else None
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits


def compute_send_time(settings: dict, lead_id: str, start_on: date, day_offset: int) -> datetime:
    """Existing cadence spreading logic, intentionally unchanged for this milestone."""
    import random

    hours = settings["business_hours"] or {}
    holidays = _parse_holidays(settings["holidays"])
    tz = ZoneInfo(settings["timezone"] or "America/Los_Angeles")
    target = start_on + timedelta(days=day_offset)
    for _ in range(21):
        window = hours.get(str(target.isoweekday()))
        holiday = holidays.get(target)
        if window and holiday is not False:
            open_t = _parse_time(window["open"])
            close_t = _parse_time(holiday) if isinstance(holiday, str) else _parse_time(window["close"])
            open_dt = datetime.combine(target, open_t, tzinfo=tz)
            close_dt = datetime.combine(target, close_t, tzinfo=tz)
            usable = max((close_dt - open_dt).total_seconds() - 1800, 0)
            slot = (hash(str(lead_id)) % 10_000) / 10_000
            offset = max(0, min(slot * usable + random.uniform(-120, 120), usable))
            return (open_dt + timedelta(seconds=offset)).astimezone(UTC)
        target += timedelta(days=1)
    return datetime.combine(target, dtime(9, 0), tzinfo=tz).astimezone(UTC)


def _parse_holidays(raw) -> dict:
    out = {}
    for item in raw or []:
        if isinstance(item, str):
            out[date.fromisoformat(item)] = False
        elif isinstance(item, dict) and "date" in item:
            out[date.fromisoformat(item["date"])] = item.get("close", False)
    return out


def _parse_time(value: str) -> dtime:
    hours, minutes = value.split(":")[:2]
    return dtime(int(hours), int(minutes))


# Minutes between two steps that fall on the same cadence day.
STEP_GAP_MINUTES = 3


def materialize_cadence(
    conn,
    lead_id: str,
    practice_id: int,
    start_on: date,
    cadence_version_id: int | None = None,
    update_lead: bool = True,
) -> int:
    """Materialize production cadence or a compressed cadence for synthetic test leads."""
    settings = conn.execute(
        "select ps.business_hours,ps.holidays,p.timezone from practice_settings ps "
        "join practices p on p.id=ps.practice_id where ps.practice_id=%s", (practice_id,),
    ).fetchone()
    if not settings:
        raise ValueError("practice settings not found")
    lead = conn.execute(
        "select is_test,cadence_state from leads where id=%s", (lead_id,)
    ).fetchone()
    # The optional version id needs an explicit cast. Passing None leaves
    # Postgres with a bare NULL it cannot type ("could not determine data type
    # of parameter $2"), which broke every caller that does not name a version:
    # creating a lead, and restarting one from the board.
    version = conn.execute(
        "select id from cadence_versions where practice_id=%s and status='active' "
        "and (%s::bigint is null or id=%s::bigint) and (lead_id=%s or lead_id is null) "
        "order by (lead_id is not null) desc limit 1",
        (practice_id, cadence_version_id, cadence_version_id, lead_id),
    ).fetchone()
    if not version:
        return 0
    steps = conn.execute(
        "select id,step_order,day_offset,channel from cadence_steps "
        "where cadence_version_id=%s and is_active order by day_offset,step_order",
        (version["id"],),
    ).fetchall()
    app_settings = get_settings()
    accelerated = bool(app_settings.test_mode and lead and lead["is_test"])
    anchor = datetime.now(UTC)
    # Steps sharing a day were spaced one second apart, so the day-0 SMS went out
    # in the same worker tick as the day-0 call: a patient who answered and asked
    # for a callback had already been texted. Spacing later steps by whole minutes
    # gives the call time to finish and settle first.
    # step_order runs across the whole cadence, so the gap is counted per day:
    # the first step of a day is unshifted, the second is STEP_GAP_MINUTES later.
    seen_on_day: dict[int, int] = {}
    base_by_day: dict[int, datetime] = {}
    previous: datetime | None = None
    for step in steps:
        position = seen_on_day.get(step["day_offset"], 0)
        seen_on_day[step["day_offset"]] = position + 1
        gap = timedelta(minutes=STEP_GAP_MINUTES * position)
        if step["day_offset"] not in base_by_day:
            base_by_day[step["day_offset"]] = (
                anchor
                + timedelta(minutes=step["day_offset"] * app_settings.test_cadence_day_minutes)
                if accelerated
                else compute_send_time(settings, lead_id, start_on, step["day_offset"])
            )
        scheduled_for = base_by_day[step["day_offset"]] + gap
        # A three-minute same-day gap is longer than a one-minute compressed
        # cadence day. Keep synthetic execution ordered even in that case.
        if accelerated and previous and scheduled_for <= previous:
            scheduled_for = previous + timedelta(seconds=1)
        previous = scheduled_for
        conn.execute(
            "insert into outreach_events(lead_id,cadence_step_id,cadence_version_id,channel,"
            "day_offset,scheduled_for,status) values(%s,%s,%s,%s,%s,%s,'planned')",
            (
                lead_id,
                step["id"],
                version["id"],
                step["channel"],
                step["day_offset"],
                scheduled_for,
            ),
        )
    if update_lead:
        conn.execute(
            "update leads set cadence_started_on=%s,cadence_state='active',status='in_progress',"
            "status_changed_at=now() where id=%s", (start_on, lead_id),
        )
    return len(steps)


CLAIM_SQL = """
with due as (
 select oe.id from outreach_events oe
 join leads l on l.id=oe.lead_id
 join practices p on p.id=l.practice_id
 join practice_settings ps on ps.practice_id=l.practice_id
 where oe.status='planned' and oe.scheduled_for<=now() and l.cadence_state='active'
 and l.status not in ('booked','declined','do_not_contact','invalid_phone')
 and ((oe.channel='call' and not l.call_opt_out) or (oe.channel='sms' and not l.sms_opt_out))
 and (oe.channel<>'call' or coalesce(l.line_type,'unknown')<>'mobile' or l.consent_captured_at is not null)
 and not exists(select 1 from suppressed_numbers s where s.phone_e164=l.phone_e164)
 and ((%(test_mode)s and l.is_test) or (
   -- practice_settings.business_hours is the single source of truth for when we may
   -- contact anyone. A day whose key is JSON null (weekends here) is closed, so
   -- jsonb_typeof rather than IS NOT NULL is what excludes it.
   jsonb_typeof(ps.business_hours -> to_char(now() at time zone coalesce(l.timezone,p.timezone),'ID')) = 'object'
   and (now() at time zone coalesce(l.timezone,p.timezone))::time
       >= ((ps.business_hours -> to_char(now() at time zone coalesce(l.timezone,p.timezone),'ID')) ->> 'open')::time
   and (now() at time zone coalesce(l.timezone,p.timezone))::time
       < ((ps.business_hours -> to_char(now() at time zone coalesce(l.timezone,p.timezone),'ID')) ->> 'close')::time
 ))
 and ((%(test_mode)s and l.is_test) or (
   oe.channel<>'call' or (
     select count(*) from outreach_events day_event
     where day_event.lead_id=l.id and day_event.channel='call' and day_event.executed_at is not null
     and day_event.executed_at >= (
       date_trunc('day',now() at time zone coalesce(l.timezone,p.timezone))
       at time zone coalesce(l.timezone,p.timezone)
     )
   ) < ps.max_calls_per_lead_per_day
 ))
 and ((%(test_mode)s and l.is_test) or (
   oe.channel<>'sms' or (
     select count(*) from outreach_events day_event
     where day_event.lead_id=l.id and day_event.channel='sms' and day_event.executed_at is not null
     and day_event.executed_at >= (
       date_trunc('day',now() at time zone coalesce(l.timezone,p.timezone))
       at time zone coalesce(l.timezone,p.timezone)
     )
   ) < ps.max_sms_per_lead_per_day
 ))
 order by oe.scheduled_for limit %(limit)s for update of oe skip locked
)
update outreach_events oe set status='in_flight',updated_at=now()
from due where oe.id=due.id
  returning oe.id,oe.lead_id,oe.channel,oe.cadence_step_id,oe.day_offset,oe.attempt_no
"""


def claim_jobs(trace: WorkflowTrace, limit: int = 20) -> list[Job]:
    trace.log("database_operation_started", operation="claim_due_events")
    with transaction() as conn:
        rows = conn.execute(
            CLAIM_SQL, {"limit": limit, "test_mode": get_settings().test_mode}
        ).fetchall()
        jobs = []
        for row in rows:
            context = conn.execute(
                "select l.full_name,l.phone_e164,ps.vapi_assistant_id,ps.vapi_phone_number_id,"
                "ps.booking_link_url,l.date_of_birth,ps.stride_case_title,ps.stride_location_id,"
                "coalesce(lmo.body,mt.body) as body "
                "from leads l "
                "join practice_settings ps on ps.practice_id=l.practice_id "
                "left join message_templates mt on mt.cadence_step_id=%s and mt.is_active "
                "left join lead_message_overrides lmo on lmo.lead_id=l.id "
                "and lmo.message_template_id=mt.id where l.id=%s limit 1",
                (row["cadence_step_id"], row["lead_id"]),
            ).fetchone()
            jobs.append(Job(
                row["id"], str(row["lead_id"]), row["channel"], context["phone_e164"],
                context["full_name"], context["body"], context["booking_link_url"], row["day_offset"],
                context["vapi_assistant_id"] or get_settings().vapi_assistant_id,
                context["vapi_phone_number_id"] or get_settings().vapi_phone_number_id,
                row["attempt_no"],
                context["date_of_birth"],
                context["stride_case_title"],
                context["stride_location_id"],
            ))
    trace.log("database_operation_completed", operation="claim_due_events", job_count=len(jobs))
    return jobs


def render_sms_template(job: Job) -> str:
    first_name = job.name.split()[0] if job.name else ""
    return (job.body or "").replace("{name}", first_name).replace(
        "{link}", job.booking_link_url or ""
    ).strip()


def dispatch_job(trace: WorkflowTrace, job: Job, providers: ProviderClients) -> DispatchResult:
    child = WorkflowTrace("outreach_dispatch", "worker", trace.trace_id)
    try:
        if job.channel == "call":
            if not job.vapi_assistant_id or not job.vapi_phone_number_id:
                raise ProviderError(
                    "vapi", "missing_configuration",
                    "Vapi assistant id and phone number id are required",
                )
            ref = providers.create_vapi_call(child, {
                "assistantId": job.vapi_assistant_id, "phoneNumberId": job.vapi_phone_number_id,
                "customer": {"number": job.phone}, "assistantOverrides": {"variableValues": {
                    # Vapi variable names are case-sensitive: the assistant prompt reads
                    # Patient_name and call_attempt, so both spellings are sent.
                    "Patient_name": job.name, "call_attempt": str(job.attempt_no),
                    "lead_id": job.lead_id, "outreach_event_id": str(job.event_id), "patient_name": job.name,
                    "booking_link": job.booking_link_url or "", "day_offset": job.day_offset,
                    "date_of_birth": job.date_of_birth.isoformat() if job.date_of_birth else "",
                    "case_title": job.case_title,
                    "location": str(job.location_id or ""),
                }},
            })
        else:
            body = render_sms_template(job)
            if not body:
                raise ProviderError("twilio", "missing_template", "SMS template is missing")
            ref = providers.send_sms(child, job.phone, body)
        child.complete(provider_ref=ref)
        return DispatchResult(job, "accepted", ref)
    except ProviderError as exc:
        child.fail(exc)
        state = "retry" if exc.retryable else ("unknown" if exc.ambiguous else "failed")
        return DispatchResult(job, state, str(exc), exc.retry_after_seconds)
    except Exception as exc:  # noqa: BLE001 - isolate one dispatch with an unknown outcome
        child.fail(exc)
        return DispatchResult(
            job,
            "unknown",
            f"unexpected dispatch error: {type(exc).__name__}",
        )


def run_safety_checks(trace: WorkflowTrace) -> dict[str, int]:
    """Flag ambiguous work for review; never blindly resend it."""
    counts = {
        "stuck_dispatches": 0,
        "orphaned_calls": 0,
        "stuck_notifications": 0,
        "stuck_handoffs": 0,
        "stuck_bookings": 0,
        "exhausted_leads": 0,
    }
    with transaction() as conn:
        stuck = conn.execute(
            "update outreach_events set status='unknown',settled_at=now(),settled_by='sweeper',"
            "failure_reason='worker stopped before dispatch result was recorded' "
            "where status='in_flight' and updated_at<now()-interval '15 minutes' returning lead_id"
        ).fetchall()
        orphaned = conn.execute(
            "update outreach_events set status='delivered',settled_at=now(),settled_by='sweeper',outcome='manual',"
            "failure_reason='call outcome was not reported' where status='attempted' "
            "and executed_at<now()-interval '2 hours' returning lead_id,vapi_call_id"
        ).fetchall()
        for row in stuck:
            conn.execute(
                "update leads set needs_review=true,review_reason=%s,review_flagged_at=now() where id=%s",
                ("ambiguous provider dispatch; do not retry", row["lead_id"]),
            )
        for row in orphaned:
            # A lead whose cadence already ended has nothing left to act on, so
            # settling its stale event must not drag it back into the review queue.
            conn.execute(
                "update leads set needs_review=true,review_reason=%s,review_flagged_at=now() "
                "where id=%s and cadence_state not in ('completed','terminated')",
                ("call outcome not reported", row["lead_id"]),
            )
            if row["vapi_call_id"]:
                conn.execute(
                    "update test_usage_ledger set status='ended',outcome='manual',"
                    "finalized_at=coalesce(finalized_at,now()) "
                    "where provider='vapi' and provider_ref=%s",
                    (row["vapi_call_id"],),
                )
        stuck_notifications = conn.execute(
            "update notification_log set status='unknown',error=%s,updated_at=now() "
            "where status='sending' and updated_at<now()-interval '15 minutes' returning lead_id",
            ("worker stopped after SMS send began; do not retry",),
        ).fetchall()
        for row in stuck_notifications:
            if row["lead_id"]:
                conn.execute(
                    "update leads set needs_review=true,review_reason=%s,review_flagged_at=now() where id=%s",
                    ("ambiguous confirmation SMS; do not retry", row["lead_id"]),
                )
        stuck_handoffs = conn.execute(
            "update integration_outbox set status='pending',next_attempt_at=now(),last_error=%s,updated_at=now() "
            "where status='sending' and updated_at<now()-interval '15 minutes' returning id",
            ("worker stopped during delivery; retry with the same event_id",),
        ).fetchall()
        stuck_bookings = conn.execute(
            "update appointments set state='unknown',needs_staff_review=true,stride_error=%s,"
            "updated_at=now() where state='booking' and updated_at<now()-interval '15 minutes' "
            "returning lead_id",
            ("booking did not reach a durable result; reconcile with Stride before retry",),
        ).fetchall()
        for row in stuck_bookings:
            conn.execute(
                "update leads set needs_review=true,review_reason=%s,review_flagged_at=now() where id=%s",
                ("stale Stride booking requires reconciliation", row["lead_id"]),
            )
        # A cadence is spent when nothing is left to send and nothing is still
        # out with a provider -- not when the calendar says so. The old rule also
        # required cadence_started_on+14 days, which described the standard
        # cadence rather than the one the lead is actually on: a compressed test
        # run stayed 'active' for a fortnight with an empty schedule, and a real
        # lead whose last step falls on day 13 waited an extra day to close.
        #
        # Leaving those leads active is not cosmetic. Activating a cadence
        # version replans every lead that is active or paused, so a finished
        # lead would be dialled again from day 0.
        #
        # Deliberately no reference to test mode: the same condition is correct
        # whether the cadence took thirteen minutes or thirteen days.
        exhausted = conn.execute(
            "update leads l set status='closed_no_response',cadence_state='completed',status_changed_at=now() "
            "where l.cadence_state='active' "
            "and exists(select 1 from outreach_events oe where oe.lead_id=l.id) "
            "and not exists(select 1 from outreach_events oe where oe.lead_id=l.id "
            "and oe.status in ('planned','in_flight','attempted')) returning id"
        ).fetchall()
        counts.update(
            stuck_dispatches=len(stuck),
            orphaned_calls=len(orphaned),
            stuck_notifications=len(stuck_notifications),
            stuck_handoffs=len(stuck_handoffs),
            stuck_bookings=len(stuck_bookings),
            exhausted_leads=len(exhausted),
        )
    trace.log("safety_checks_completed", **counts)
    return counts


# The sweeper only looks for work older than two hours, so running it on every
# poll costs a round trip to the database for nothing. Once every ten minutes
# still settles a stalled call long before anyone notices it.
SAFETY_CHECK_INTERVAL_SECONDS = 600
_last_safety_check = 0.0


def run_tick() -> dict[str, int]:
    global _last_safety_check
    trace = WorkflowTrace("worker_tick", "worker", uuid4().hex)
    providers = ProviderClients()
    now = time.monotonic()
    if now - _last_safety_check >= SAFETY_CHECK_INTERVAL_SECONDS:
        _last_safety_check = now
        run_safety_checks(trace)
    reprocess_failed_vapi_events(trace)
    reprocess_failed_twilio_events(trace)
    # Claiming while suspended would burn each event against the provider guard
    # and mark it failed. Leave the queue untouched so it resumes intact.
    jobs = claim_jobs(trace) if get_settings().outbound_enabled else []
    if not jobs and not get_settings().outbound_enabled:
        trace.log("outbound_suspended")
    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(dispatch_job, trace, job, providers) for job in jobs]
        results.extend(future.result() for future in as_completed(futures))
    counts = {"accepted": 0, "retried": 0, "failed": 0, "unknown": 0}
    settings = get_settings()
    with transaction() as conn:
        for result in results:
            job, state, value = result.job, result.state, result.value
            if state == "accepted" and job.channel == "call":
                conn.execute(
                    "update outreach_events set status=case when status='in_flight' then 'attempted' else status end,"
                    "executed_at=coalesce(executed_at,now()),provider='vapi',provider_ref=%s,vapi_call_id=%s "
                    "where id=%s and status in ('in_flight','delivered')", (value, value, job.event_id),
                )
                conn.execute(
                    "update leads set call_attempts=call_attempts+1,last_contacted_at=now() where id=%s",
                    (job.lead_id,),
                )
                if providers.settings.mode("vapi") == "real":
                    record_test_usage(conn, "vapi", "call", job.lead_id, value)
            elif state == "accepted":
                conn.execute(
                    "update outreach_events set status='delivered',executed_at=now(),settled_at=now(),"
                    "settled_by='worker',provider='twilio',provider_ref=%s where id=%s and status='in_flight'",
                    (value, job.event_id),
                )
                conn.execute(
                    "insert into sms_messages(lead_id,outreach_event_id,direction,body,occurred_at,delivery_status,"
                    "provider_message_id) values(%s,%s,'outbound',%s,now(),'queued',%s) "
                    "on conflict(provider_message_id) do nothing",
                    (job.lead_id, job.event_id, render_sms_template(job), value),
                )
                conn.execute(
                    "update leads set last_contacted_at=now() where id=%s", (job.lead_id,)
                )
                if providers.settings.mode("twilio") == "real":
                    record_test_usage(conn, "twilio", "cadence_sms", job.lead_id, value)
            elif state == "retry" and job.attempt_no < settings.retry_max_attempts:
                delay = retry_delay_seconds(
                    job.attempt_no,
                    result.retry_after_seconds,
                    settings,
                )
                conn.execute(
                    "update outreach_events set status='planned',scheduled_for=now()+make_interval(secs=>%s),"
                    "attempt_no=attempt_no+1,failure_reason=%s,updated_at=now() "
                    "where id=%s and status='in_flight'",
                    (delay, value[:500], job.event_id),
                )
                trace.log(
                    "outreach_retry_scheduled",
                    event_id=job.event_id,
                    attempt=job.attempt_no,
                    retry_in_seconds=delay,
                )
                counts["retried"] += 1
            else:
                exhausted = state == "retry"
                conn.execute(
                    "update outreach_events set status=%s,executed_at=now(),settled_at=now(),settled_by='worker',"
                    "failure_reason=%s where id=%s and status='in_flight'",
                    ("failed" if state in {"failed", "retry"} else "unknown", value[:500], job.event_id),
                )
                conn.execute(
                    "update leads set needs_review=true,review_reason=%s,review_flagged_at=now() where id=%s",
                    (
                        f"dispatch retries exhausted: {value}"
                        if exhausted else (
                            f"ambiguous dispatch: {value}"
                            if state == "unknown" else f"dispatch failed: {value}"
                        ),
                        job.lead_id,
                    ),
                )
                counts["failed" if state in {"failed", "retry"} else "unknown"] += 1
            if state == "accepted":
                counts["accepted"] += 1
    process_pending_integrations(trace, providers)
    trace.complete(**counts)
    return counts


def main() -> None:
    configure_logging("worker")
    settings = get_settings()
    errors = settings.runtime_errors("worker")
    if errors:
        raise RuntimeError("; ".join(errors))
    interval = settings.worker_poll_seconds
    logging.getLogger(__name__).info("worker_started", extra={"event": "worker_started"})
    while True:
        started = time.monotonic()
        try:
            run_tick()
        except Exception:
            logging.getLogger(__name__).exception("worker_tick_failed", extra={"event": "worker_tick_failed"})
        time.sleep(max(0, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
