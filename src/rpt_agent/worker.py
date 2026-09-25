from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg

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
from .services.review import flag_lead_for_review
from .services.sheet_sync import enqueue_sheet_update
from .usage_report import record_test_usage

# Session-level advisory lock: only one cadence worker may dispatch outreach.
CADENCE_WORKER_LOCK_ID = 7_272_054_119


@contextmanager
def cadence_worker_lock(database_url: str, connect_timeout: float):
    """Hold a database-wide singleton lock for this worker process."""
    with psycopg.connect(
        database_url,
        autocommit=True,
        connect_timeout=max(1, int(connect_timeout)),
    ) as conn:
        acquired = conn.execute(
            "select pg_try_advisory_lock(%s)", (CADENCE_WORKER_LOCK_ID,)
        ).fetchone()[0]
        if not acquired:
            raise RuntimeError("another cadence worker already owns the worker lock")
        try:
            yield conn
        finally:
            conn.execute("select pg_advisory_unlock(%s)", (CADENCE_WORKER_LOCK_ID,))


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
    # Production keeps the established same-day spacing. A compressed test day is
    # literal: every step on Day 0 starts at the anchor, Day 1 is one configured
    # test-day later, and so on. claim_jobs serializes each lead so equal timestamps
    # cannot make an SMS overtake an unresolved call.
    seen_on_day: dict[int, int] = {}
    base_by_day: dict[int, datetime] = {}
    previous: datetime | None = None
    for step in steps:
        position = seen_on_day.get(step["day_offset"], 0)
        seen_on_day[step["day_offset"]] = position + 1
        gap = timedelta(minutes=0 if accelerated else STEP_GAP_MINUTES * position)
        if step["day_offset"] not in base_by_day:
            base_by_day[step["day_offset"]] = (
                anchor
                + timedelta(minutes=step["day_offset"] * app_settings.test_cadence_day_minutes)
                if accelerated
                else compute_send_time(settings, lead_id, start_on, step["day_offset"])
            )
        scheduled_for = base_by_day[step["day_offset"]] + gap
        if accelerated and previous and scheduled_for < previous:
            scheduled_for = previous
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
with eligible as (
 select oe.id,oe.lead_id,oe.scheduled_for,
 row_number() over(partition by oe.lead_id order by oe.scheduled_for,oe.id) as lead_order
 from outreach_events oe
 join leads l on l.id=oe.lead_id
 join practices p on p.id=l.practice_id
 join practice_settings ps on ps.practice_id=l.practice_id
 where oe.status='planned' and oe.scheduled_for<=now() and l.cadence_state='active'
 and not l.needs_review
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
     where day_event.lead_id in (select peer.id from leads peer where peer.practice_id=l.practice_id and peer.phone_e164=l.phone_e164)
     and day_event.channel='call' and day_event.executed_at is not null
     and day_event.executed_at >= (
       date_trunc('day',now() at time zone coalesce(l.timezone,p.timezone))
       at time zone coalesce(l.timezone,p.timezone)
     )
   ) < ps.max_calls_per_lead_per_day
 ))
 and ((%(test_mode)s and l.is_test) or (
   oe.channel<>'sms' or (
     select count(*) from outreach_events day_event
     where day_event.lead_id in (select peer.id from leads peer where peer.practice_id=l.practice_id and peer.phone_e164=l.phone_e164)
     and day_event.channel='sms' and day_event.executed_at is not null
     and day_event.executed_at >= (
       date_trunc('day',now() at time zone coalesce(l.timezone,p.timezone))
       at time zone coalesce(l.timezone,p.timezone)
     )
   ) < ps.max_sms_per_lead_per_day
  ))
 and not exists(
   select 1 from outreach_events active_event
   where active_event.lead_id=oe.lead_id and active_event.status in ('in_flight','attempted')
 )
), due as (
 select oe.id from outreach_events oe
 join eligible e on e.id=oe.id and e.lead_order=1
 order by oe.scheduled_for,oe.id limit %(limit)s for update of oe skip locked
)
update outreach_events oe set status='in_flight',updated_at=now()
from due where oe.id=due.id
  returning oe.id,oe.lead_id,oe.channel,oe.cadence_step_id,oe.day_offset,oe.attempt_no
"""


def claim_jobs(trace: WorkflowTrace, limit: int = 20) -> list[Job]:
    trace.log("database_operation_started", operation="claim_due_events")
    with transaction() as conn:
        # Migration 025 permits only workers that identify with this claim protocol.
        # Stale deployments are unable to move planned outreach to in_flight.
        conn.execute("select set_config('rpt.worker_claim_protocol','v1',true)")
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
        "stale_sms": 0,
        "stuck_notifications": 0,
        "stuck_handoffs": 0,
        "stuck_bookings": 0,
        "exhausted_leads": 0,
    }
    with transaction() as conn:
        stuck = conn.execute(
            "update outreach_events set status='unknown',settled_at=now(),settled_by='sweeper',"
            "failure_reason='worker stopped before dispatch result was recorded' "
            "where status='in_flight' and updated_at<now()-interval '15 minutes' "
            "returning id,lead_id"
        ).fetchall()
        orphaned = conn.execute(
            "update outreach_events set status='delivered',settled_at=now(),settled_by='sweeper',outcome='manual',"
            "failure_reason='call outcome was not reported' where channel='call' and status='attempted' "
            "and executed_at<now()-interval '2 hours' returning id,lead_id,vapi_call_id"
        ).fetchall()
        stale_sms = conn.execute(
            "update outreach_events set status='unknown',settled_at=now(),settled_by='sweeper',"
            "failure_reason='SMS delivery callback was not reported' "
            "where channel='sms' and status='attempted' "
            "and executed_at<now()-interval '2 hours' returning id,lead_id"
        ).fetchall()
        for row in stuck:
            flag_lead_for_review(
                conn, row["lead_id"], "ambiguous provider dispatch; do not retry"
            )
            enqueue_sheet_update(
                conn,
                lead_id=str(row["lead_id"]),
                event_type="outreach_unknown",
                source_key=f"event:{row['id']}:stuck",
                outreach_event_id=row["id"],
            )
        for row in orphaned:
            # A lead whose cadence already ended has nothing left to act on, so
            # settling its stale event must not drag it back into the review queue.
            flag_lead_for_review(conn, row["lead_id"], "call outcome not reported")
            if row["vapi_call_id"]:
                conn.execute(
                    "update test_usage_ledger set status='ended',outcome='manual',"
                    "finalized_at=coalesce(finalized_at,now()) "
                    "where provider='vapi' and provider_ref=%s",
                    (row["vapi_call_id"],),
                )
            enqueue_sheet_update(
                conn,
                lead_id=str(row["lead_id"]),
                event_type="call_settled",
                source_key=f"event:{row['id']}:manual",
                outreach_event_id=row["id"],
            )
        for row in stale_sms:
            flag_lead_for_review(conn, row["lead_id"], "SMS delivery status not reported")
            enqueue_sheet_update(
                conn,
                lead_id=str(row["lead_id"]),
                event_type="outreach_unknown",
                source_key=f"event:{row['id']}:sms-callback-missing",
                outreach_event_id=row["id"],
            )
        stuck_notifications = conn.execute(
            "update notification_log set status='unknown',error=%s,updated_at=now() "
            "where status='sending' and updated_at<now()-interval '15 minutes' returning lead_id",
            ("worker stopped after SMS send began; do not retry",),
        ).fetchall()
        for row in stuck_notifications:
            if row["lead_id"]:
                flag_lead_for_review(
                    conn, row["lead_id"], "ambiguous confirmation SMS; do not retry"
                )
        stuck_handoffs = conn.execute(
            "update integration_outbox set status='pending',next_attempt_at=now(),last_error=%s,updated_at=now() "
            "where destination='keap' and status='sending' "
            "and updated_at<now()-interval '15 minutes' returning id",
            ("worker stopped during delivery; retry with the same event_id",),
        ).fetchall()
        stuck_bookings = conn.execute(
            "update appointments set state='unknown',needs_staff_review=true,stride_error=%s,"
            "updated_at=now() where state='booking' and updated_at<now()-interval '15 minutes' "
            "returning lead_id",
            ("booking did not reach a durable result; reconcile with Stride before retry",),
        ).fetchall()
        for row in stuck_bookings:
            flag_lead_for_review(
                conn, row["lead_id"], "stale Stride booking requires reconciliation"
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
        for row in exhausted:
            enqueue_sheet_update(
                conn,
                lead_id=str(row["id"]),
                event_type="cadence_finished",
                source_key=f"lead:{row['id']}:closed_no_response",
            )
        counts.update(
            stuck_dispatches=len(stuck),
            orphaned_calls=len(orphaned),
            stale_sms=len(stale_sms),
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
                mock_delivery = providers.settings.mode("twilio") == "mock"
                conn.execute(
                    "update outreach_events set status=%s,executed_at=now(),"
                    "settled_at=case when %s then now() else null end,"
                    "settled_by=case when %s then 'worker' else null end,"
                    "provider='twilio',provider_ref=%s where id=%s and status='in_flight'",
                    (
                        "delivered" if mock_delivery else "attempted",
                        mock_delivery,
                        mock_delivery,
                        value,
                        job.event_id,
                    ),
                )
                conn.execute(
                    "insert into sms_messages(lead_id,outreach_event_id,direction,body,occurred_at,delivery_status,"
                    "provider_message_id,delivered_at) values(%s,%s,'outbound',%s,now(),%s,%s,"
                    "case when %s then now() else null end) "
                    "on conflict(provider_message_id) do nothing",
                    (
                        job.lead_id,
                        job.event_id,
                        render_sms_template(job),
                        "delivered" if mock_delivery else "queued",
                        value,
                        mock_delivery,
                    ),
                )
                conn.execute(
                    "update leads set last_contacted_at=now() where id=%s", (job.lead_id,)
                )
                if providers.settings.mode("twilio") == "real":
                    record_test_usage(conn, "twilio", "cadence_sms", job.lead_id, value)
                else:
                    enqueue_sheet_update(
                        conn,
                        lead_id=job.lead_id,
                        event_type="sms_settled",
                        source_key=f"{value}:delivered",
                        outreach_event_id=job.event_id,
                    )
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
                flag_lead_for_review(
                    conn,
                    job.lead_id,
                    f"dispatch retries exhausted: {value}"
                    if exhausted else (
                        f"ambiguous dispatch: {value}"
                        if state == "unknown" else f"dispatch failed: {value}"
                    ),
                )
                # Migration 025 creates the Sheet outbox row from the terminal
                # status transition, including transitions made by stale workers.
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
    with cadence_worker_lock(settings.supabase_db_url, settings.db_pool_timeout_seconds) as lock_conn:
        logging.getLogger(__name__).info("worker_started", extra={"event": "worker_started"})
        while True:
            started = time.monotonic()
            # A lost lock connection releases the lock. Exit instead of
            # continuing to dispatch without ownership.
            lock_conn.execute("select 1")
            try:
                run_tick()
            except Exception:
                logging.getLogger(__name__).exception(
                    "worker_tick_failed", extra={"event": "worker_tick_failed"}
                )
            time.sleep(max(0, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
