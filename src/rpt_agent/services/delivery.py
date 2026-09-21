from __future__ import annotations

from zoneinfo import ZoneInfo

from ..config import get_settings
from ..db import transaction
from ..observability import WorkflowTrace
from ..providers import ProviderClients, ProviderError
from ..retry import retry_delay_seconds
from ..usage_report import record_test_usage
from ..vapi_contract import extract_vapi_context, outcome_from_ended_reason
from .lead_status import apply_call_outcome
from .sheet_sync import dashboard_call_link, enqueue_sheet_update

# The patient's last word on the call wins. A booking link waits two minutes
# after it is requested; if by then the lead is no longer booking_link_sent
# (they declined, asked for a callback, were transferred, or staff changed the
# lead) the text is never sent.
CANCEL_CHANGED_BOOKING_LINK_SQL = (
    "update notification_log n set status='skipped',error=%s,updated_at=now() "
    "from leads l where n.lead_id=l.id and n.status='queued' "
    "and n.notification_type='sms_booking_link' and l.status<>'booking_link_sent'"
)


def process_pending_integrations(
    trace: WorkflowTrace, providers: ProviderClients | None = None
) -> dict[str, int]:
    providers = providers or ProviderClients()
    settings = providers.settings
    counts = {"sms": 0, "handoff": 0, "retried": 0, "dead": 0, "failed": 0}
    with transaction() as conn:
        conn.execute(
            "update notification_log n set status='skipped',error=%s,updated_at=now() "
            "from leads l where n.lead_id=l.id and n.status='queued' and (l.sms_opt_out or exists("
            "select 1 from suppressed_numbers s where s.phone_e164=l.phone_e164))",
            ("notification canceled because the recipient is opted out or suppressed",),
        )
        conn.execute(
            CANCEL_CHANGED_BOOKING_LINK_SQL,
            ("booking link canceled because the patient changed their decision",),
        )
        notifications = conn.execute(
            "select n.id,n.lead_id,n.appointment_id,n.notification_type,n.payload,n.attempts,"
            "l.phone_e164,l.first_name,a.start_utc,"
            "coalesce(ps.stride_location_timezone,l.timezone,'America/Los_Angeles') "
            "as stride_location_timezone from notification_log n join leads l on l.id=n.lead_id "
            "left join appointments a on a.id=n.appointment_id "
            "join practice_settings ps on ps.practice_id=l.practice_id "
            "where n.status='queued' and n.next_attempt_at<=now() "
            "and not l.sms_opt_out and not exists("
            "select 1 from suppressed_numbers s where s.phone_e164=l.phone_e164) "
            "order by n.id limit 20 for update of n skip locked"
        ).fetchall()
        for row in notifications:
            conn.execute(
                "update notification_log set status='sending',attempts=attempts+1,updated_at=now() "
                "where id=%s",
                (row["id"],),
            )
        outbox = conn.execute(
            "select id,payload,attempts from integration_outbox "
            "where destination='keap' and status='pending' and next_attempt_at<=now() "
            "order by id limit 20 for update skip locked"
        ).fetchall()
        for row in outbox:
            conn.execute(
                "update integration_outbox set status='sending',attempts=attempts+1,updated_at=now() "
                "where id=%s",
                (row["id"],),
            )
    for row in notifications:
        try:
            greeting = f"Hi {row['first_name']}, " if row["first_name"] else ""
            if row["notification_type"] == "sms_booking_link":
                link = (row["payload"] or {}).get("booking_link_url", "")
                if not link:
                    raise ValueError("booking link notification has no URL")
                # Wording carried over from the approved outreach copy patients
                # already receive; the STOP line is required and is our addition.
                body = (
                    f"Hi {row['first_name'] or 'there'},\n\n"
                    "We're excited to help you get started on your wellness journey with "
                    "Rausch PT & Wellness. You can schedule your first physical therapy "
                    f"session anytime using this link:\n\n{link}\n\n"
                    "If you have any trouble scheduling, have questions about which service "
                    "to choose, or don't see a time that works for you, please call us at "
                    "949-276-5401 and our team will be happy to help.\n\n"
                    "Kind regards,\nThe Rausch PT & Wellness Team\n\n"
                    "Reply STOP to opt out."
                )
            else:
                local_start = row["start_utc"].astimezone(
                    ZoneInfo(row["stride_location_timezone"])
                )
                when = local_start.strftime("%A, %B %d at %I:%M %p").replace(" 0", " ")
                body = (
                    f"{greeting}your Rausch PT appointment is confirmed for {when}. "
                    "Call 949-276-5401 with questions. Reply STOP to opt out."
                )
            sid = providers.send_sms(trace, row["phone_e164"], body)
            # The message has left Twilio, so recording that fact comes first and
            # alone. Usage accounting is bookkeeping: if it fails it must not roll
            # back the row proving we already sent this, or the worker will treat
            # a delivered message as unsent.
            with transaction() as conn:
                conn.execute(
                    "update notification_log set status='sent',provider_ref=%s,sent_at=now(),"
                    "updated_at=now() where id=%s",
                    (sid, row["id"]),
                )
                # notification_log is the outbox -- retries, attempts, next
                # attempt. sms_messages is the conversation the patient sees,
                # and it is what the dashboard reads. Writing only the first
                # left a hole: we texted someone a booking link and their
                # thread showed nothing, so a reply arrived with no outbound
                # message above it.
                #
                # Same sid in both, which the Twilio status webhook already
                # matches on (sms_messages.provider_message_id and
                # notification_log.provider_ref), so delivery status tracks
                # itself with no extra work.
                conn.execute(
                    "insert into sms_messages(lead_id,direction,body,occurred_at,delivery_status,"
                    "provider_message_id) values(%s,'outbound',%s,now(),'queued',%s) "
                    "on conflict (provider_message_id) do nothing",
                    (row["lead_id"], body, sid),
                )
            if providers.settings.mode("twilio") == "real":
                try:
                    with transaction() as conn:
                        record_test_usage(
                            conn,
                            "twilio",
                            "booking_link_sms"
                            if row["notification_type"] == "sms_booking_link"
                            else "booking_confirmation_sms",
                            row["lead_id"],
                            sid,
                        )
                except Exception as usage_error:  # noqa: BLE001 - never lose a sent SMS
                    trace.log(
                        "usage_recording_failed",
                        notification_id=row["id"],
                        error_category=type(usage_error).__name__,
                    )
            counts["sms"] += 1
        except ProviderError as exc:
            with transaction() as conn:
                attempt = row["attempts"] + 1
                if exc.retryable and attempt < settings.retry_max_attempts:
                    delay = retry_delay_seconds(attempt, exc.retry_after_seconds, settings)
                    conn.execute(
                        "update notification_log set status='queued',error=%s,"
                        "next_attempt_at=now()+make_interval(secs=>%s),updated_at=now() where id=%s",
                        (str(exc)[:500], delay, row["id"]),
                    )
                    counts["retried"] += 1
                    trace.log(
                        "notification_retry_scheduled",
                        notification_id=row["id"],
                        attempt=attempt,
                        retry_in_seconds=delay,
                    )
                    continue
                status = "unknown" if exc.ambiguous else "failed"
                conn.execute(
                    "update notification_log set status=%s,error=%s,updated_at=now() where id=%s",
                    (status, str(exc)[:500], row["id"]),
                )
                conn.execute(
                    "update leads set needs_review=true,review_reason=%s,review_flagged_at=now() "
                    "where id=%s",
                    (
                        "ambiguous SMS notification; reconcile before retry"
                        if exc.ambiguous else (
                            "SMS notification retries exhausted"
                            if exc.retryable else "booking link text could not be delivered"
                        ),
                        row["lead_id"],
                    ),
                )
            counts["failed"] += 1
        except Exception as exc:  # noqa: BLE001 - an accepted SMS cannot be retried safely
            trace.log(
                "integration_delivery_failed",
                provider="twilio",
                notification_id=row["id"],
                error_category=type(exc).__name__,
            )
            with transaction() as conn:
                conn.execute(
                    "update notification_log set status='unknown',error=%s,updated_at=now() where id=%s",
                    (f"unexpected delivery error: {type(exc).__name__}", row["id"]),
                )
                conn.execute(
                    "update leads set needs_review=true,review_reason=%s,review_flagged_at=now() "
                    "where id=%s",
                    ("SMS notification delivery requires review", row["lead_id"]),
                )
            counts["failed"] += 1
    for row in outbox:
        try:
            providers.deliver_handoff(trace, row["payload"])
            with transaction() as conn:
                conn.execute(
                    "update integration_outbox set status='delivered',delivered_at=now(),"
                    "updated_at=now() where id=%s", (row["id"],)
                )
            counts["handoff"] += 1
        except ProviderError as exc:
            with transaction() as conn:
                attempt = row["attempts"] + 1
                if exc.retryable and attempt < settings.retry_max_attempts:
                    delay = retry_delay_seconds(attempt, exc.retry_after_seconds, settings)
                    conn.execute(
                        "update integration_outbox set status='pending',last_error=%s,"
                        "next_attempt_at=now()+make_interval(secs=>%s),updated_at=now() where id=%s",
                        (str(exc)[:500], delay, row["id"]),
                    )
                    counts["retried"] += 1
                else:
                    conn.execute(
                        "update integration_outbox set status='dead',last_error=%s,updated_at=now() "
                        "where id=%s",
                        (str(exc)[:500], row["id"]),
                    )
                    counts["dead"] += 1
            counts["failed"] += 1
        except Exception as exc:  # noqa: BLE001 - outbox event_id makes retries idempotent
            trace.log(
                "integration_delivery_failed",
                provider="keap",
                outbox_id=row["id"],
                error_category=type(exc).__name__,
            )
            with transaction() as conn:
                attempt = row["attempts"] + 1
                if attempt < settings.retry_max_attempts:
                    delay = retry_delay_seconds(attempt, settings=settings)
                    conn.execute(
                        "update integration_outbox set status='pending',last_error=%s,"
                        "next_attempt_at=now()+make_interval(secs=>%s),updated_at=now() where id=%s",
                        (f"unexpected delivery error: {type(exc).__name__}", delay, row["id"]),
                    )
                    counts["retried"] += 1
                else:
                    conn.execute(
                        "update integration_outbox set status='dead',last_error=%s,updated_at=now() "
                        "where id=%s",
                        (f"unexpected delivery error: {type(exc).__name__}", row["id"]),
                    )
                    counts["dead"] += 1
            counts["failed"] += 1
    trace.log("integration_batch_completed", **counts)
    return counts


def _as_number(value) -> float | None:
    """Vapi sends these as numbers or strings depending on the event."""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _structured_outcome(message: dict) -> dict | None:
    """Read the assistant's post-call structured output, if it produced one.

    This is the backup for a call where the tool never fired: Vapi derives the
    same fields from the transcript, so a promised callback is still recoverable.
    """
    artifact = message.get("artifact") if isinstance(message.get("artifact"), dict) else {}
    outputs = artifact.get("structuredOutputs")
    if not isinstance(outputs, dict):
        return None
    for entry in outputs.values():
        result = entry.get("result") if isinstance(entry, dict) else None
        if isinstance(result, dict) and result.get("status"):
            return result
    return None


def _call_text_artifacts(message: dict) -> tuple[str | None, str | None]:
    artifact = message.get("artifact") if isinstance(message.get("artifact"), dict) else {}
    analysis = message.get("analysis") if isinstance(message.get("analysis"), dict) else {}
    transcript = artifact.get("transcript")
    summary = analysis.get("summary")
    return (
        transcript[:100_000] if isinstance(transcript, str) else None,
        summary[:10_000] if isinstance(summary, str) else None,
    )


def _assistant_spoke(message: dict) -> bool:
    """Whether Sarah said anything at all on the call."""
    artifact = message.get("artifact") if isinstance(message.get("artifact"), dict) else {}
    return any(
        isinstance(item, dict) and item.get("role") == "bot" and str(item.get("message") or "").strip()
        for item in artifact.get("messages") or []
    )


def _settle_from_structured_output(
    trace: WorkflowTrace, message: dict, *, lead_id: str, event_id: int, call_id: str
) -> str | None:
    """Apply the post-call structured output, or flag the call for staff.

    Never overwrites a tool result: the caller only reaches this when the event
    is still unsettled. Returns the outreach outcome, or None if nothing applied.
    """
    from .lead_status import report_lead_status  # local: lead_status imports nothing here

    result = _structured_outcome(message)
    if not result:
        with transaction() as conn:
            conn.execute(
                "update outreach_events set status='delivered',settled_at=now(),"
                "settled_by='webhook',outcome='manual',failure_reason=%s,updated_at=now() "
                "where id=%s and status in ('in_flight','attempted')",
                ("call was answered but no outcome was reported", event_id),
            )
            conn.execute(
                "update leads set needs_review=true,review_reason=%s,review_flagged_at=now() "
                "where id=%s and needs_review is not true",
                ("call answered but no outcome reported; see transcript", lead_id),
            )
        trace.log("state_transition_applied", transition="fallback_review", event_id=event_id)
        return "manual"
    try:
        report_lead_status(
            trace,
            lead_id=lead_id,
            status=str(result.get("status") or ""),
            call_id=call_id,
            event_id=event_id,
            notes=str(result.get("summary") or ""),
            callback_type=result.get("callback_type"),
            delay_minutes=result.get("delay_minutes"),
            callback_datetime_iso=result.get("callback_datetime_iso"),
            source="call summary",
        )
    except (TypeError, ValueError) as exc:
        with transaction() as conn:
            conn.execute(
                "update leads set needs_review=true,review_reason=%s,review_flagged_at=now() "
                "where id=%s",
                (f"structured outcome could not be applied: {exc}"[:500], lead_id),
            )
        trace.log("validation_failed", reason="structured_output_rejected")
        return None
    with transaction() as conn:
        row = conn.execute(
            "select outcome from outreach_events where id=%s", (event_id,)
        ).fetchone()
    trace.log("state_transition_applied", transition="fallback_structured", event_id=event_id)
    return row["outcome"] if row else None


def process_vapi_end_report(trace: WorkflowTrace, body: dict) -> str:
    """Settle one durable Vapi end report and persist its call log idempotently."""
    message = body.get("message") if isinstance(body, dict) else {}
    message = message if isinstance(message, dict) else {}
    call = message.get("call") if isinstance(message.get("call"), dict) else {}
    context = extract_vapi_context(body)
    lead_id = str(context.get("lead_id") or "")
    event_id = context.get("outreach_event_id")
    call_id = str(call.get("id") or body.get("id") or "")
    ended = str(message.get("endedReason") or call.get("endedReason") or "")
    transcript, summary = _call_text_artifacts(message)
    started_at_raw = message.get("startedAt") or call.get("startedAt")
    ended_at_raw = message.get("endedAt") or call.get("endedAt")
    duration_seconds = _as_number(message.get("durationSeconds") or call.get("durationSeconds"))
    call_cost = _as_number(message.get("cost") or call.get("cost"))
    if not lead_id or not event_id or not call_id:
        raise ValueError("webhook cannot be associated with a lead, event, and call")
    mapped_outcome = outcome_from_ended_reason(ended)
    outcome: str | None = mapped_outcome
    outcome_source: str | None = "webhook"
    if mapped_outcome == "manual":
        with transaction() as conn:
            event = conn.execute(
                "select lead_id,status,outcome from outreach_events where id=%s",
                (int(event_id),),
            ).fetchone()
        if not event or str(event["lead_id"]) != lead_id:
            raise ValueError("webhook lead and outreach event do not match")
        if event["status"] == "delivered" and event["outcome"]:
            outcome, outcome_source = event["outcome"], "tool"
        elif event["status"] in {"in_flight", "attempted"} and not _assistant_spoke(message):
            # Sarah never spoke, so the only voice on the call was a recording or
            # nobody at all: nothing on it can be the patient's decision. A
            # carrier's "forwarded to voice mail" message ended as
            # customer-ended-call, the summary read it as a decline, and the lead
            # was closed. Count it as a missed call and let the cadence go on.
            outcome = "no_answer"
            apply_call_outcome(
                trace, lead_id=lead_id, event_id=int(event_id), outcome=outcome, source="webhook"
            )
        elif event["status"] in {"in_flight", "attempted"}:
            # The tool never reported. Fall back to the structured output so a
            # callback the patient was promised is not silently lost.
            outcome = _settle_from_structured_output(
                trace, message, lead_id=lead_id, event_id=int(event_id), call_id=call_id
            )
            outcome_source = "webhook" if outcome else None
        else:
            raise ValueError("answered call report cannot settle this outreach event")
    else:
        apply_call_outcome(
            trace,
            lead_id=lead_id,
            event_id=int(event_id),
            outcome=mapped_outcome,
            source="webhook",
        )
    with transaction() as conn:
        call_log = conn.execute(
            "insert into call_logs(outreach_event_id,lead_id,vapi_call_id,dialed_at,ended_at,"
            "answer_state,ended_reason,outcome_source,transcript_text,summary_text,"
            "duration_seconds,cost) "
            "values(%s,%s,%s,coalesce(%s::timestamptz,now()),%s::timestamptz,%s,%s,%s,%s,%s,"
            # Derive the duration rather than trusting a field: we always have both
            # timestamps, and greatest() keeps a clock skew from storing a negative.
            "coalesce(%s,greatest(extract(epoch from (%s::timestamptz - %s::timestamptz)),0))::int,"
            "%s) "
            "on conflict(vapi_call_id) do update set "
            "outcome_source=coalesce(call_logs.outcome_source,excluded.outcome_source),"
            "transcript_text=coalesce(excluded.transcript_text,call_logs.transcript_text),"
            "summary_text=coalesce(excluded.summary_text,call_logs.summary_text),"
            "duration_seconds=greatest(call_logs.duration_seconds,excluded.duration_seconds),"
            "cost=coalesce(excluded.cost,call_logs.cost),"
            "ended_at=coalesce(excluded.ended_at,call_logs.ended_at) returning id",
            (
                int(event_id),
                lead_id,
                call_id,
                started_at_raw,
                ended_at_raw,
                outcome if outcome in {"voicemail", "no_answer"} else "human",
                ended,
                outcome_source,
                transcript,
                summary,
                duration_seconds,
                ended_at_raw,
                started_at_raw,
                call_cost,
            ),
        ).fetchone()
        settings = get_settings()
        transcript_link = dashboard_call_link(lead_id, settings) if transcript else None
        if transcript:
            conn.execute(
                "insert into call_transcripts(call_log_id,lead_id,transcript_text,summary,"
                "transcript_link) values(%s,%s,%s,%s,%s) "
                "on conflict(call_log_id) do update set "
                "transcript_text=excluded.transcript_text,"
                "summary=coalesce(excluded.summary,call_transcripts.summary),"
                "transcript_link=coalesce(excluded.transcript_link,call_transcripts.transcript_link)",
                (call_log["id"], lead_id, transcript, summary, transcript_link),
            )
        conn.execute(
            "update test_usage_ledger set status='ended',outcome=coalesce(%s,outcome),"
            "finalized_at=coalesce(finalized_at,now()) "
            "where provider='vapi' and provider_ref=%s",
            (outcome, call_id),
        )
        enqueue_sheet_update(
            conn,
            lead_id=lead_id,
            event_type="call_settled",
            source_key=f"{call_id}:{outcome or 'pending'}",
            outreach_event_id=int(event_id),
        )
    recorded_outcome = outcome or "pending_tool_outcome"
    trace.log("call_report_recorded", event_id=int(event_id), outcome=recorded_outcome)
    return recorded_outcome


def reprocess_failed_vapi_events(trace: WorkflowTrace) -> int:
    """Retry webhook processing only after the original payload is durable."""
    settings = get_settings()
    with transaction() as conn:
        rows = conn.execute(
            "select id,payload,processing_attempts from provider_events "
            "where provider='vapi' and processed_at is null "
            "and dead_lettered_at is null and processing_error is not null "
            "and next_attempt_at<=now() and processing_attempts<%s "
            "order by id limit 20 for update skip locked",
            (settings.retry_max_attempts,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "update provider_events set next_attempt_at=now()+interval '5 minutes' where id=%s",
                (row["id"],),
            )
    completed = 0
    for row in rows:
        try:
            body = row["payload"]
            process_vapi_end_report(trace, body)
            with transaction() as conn:
                conn.execute(
                    "update provider_events set processed_at=now(),processing_error=null where id=%s",
                    (row["id"],),
                )
            completed += 1
            trace.log("webhook_reprocessed", provider_event_id=row["id"])
        except Exception as exc:  # noqa: BLE001 - isolate malformed durable webhook records
            with transaction() as conn:
                delay = retry_delay_seconds(row["processing_attempts"] + 1, settings=settings)
                conn.execute(
                    "update provider_events set processing_attempts=processing_attempts+1,"
                    "processing_error=%s,next_attempt_at=now()+make_interval(secs=>%s),"
                    "dead_lettered_at=case when processing_attempts+1>=%s then now() else null end "
                    "where id=%s",
                    (str(exc)[:500], delay, settings.retry_max_attempts, row["id"]),
                )
            trace.log(
                "webhook_reprocess_failed",
                provider_event_id=row["id"],
                error_category=type(exc).__name__,
            )
    return completed


def apply_twilio_message_status(conn, form_data: dict[str, str]) -> int:
    sid = form_data["MessageSid"]
    mapped = form_data["MessageStatus"].lower()
    if mapped not in {"queued", "sent", "delivered", "undelivered", "failed"}:
        raise ValueError("invalid Twilio message status")
    error = form_data.get("ErrorCode") or None
    sms_result = conn.execute(
        "update sms_messages set delivery_status=case "
        "when %s='delivered' then 'delivered' when delivery_status='delivered' then delivery_status "
        "when %s in ('failed','undelivered') then %s "
        "when delivery_status in ('failed','undelivered') then delivery_status "
        "when %s='sent' then 'sent' else delivery_status end,"
        "delivered_at=case when %s='delivered' then coalesce(delivered_at,now()) else delivered_at end,"
        "failure_reason=case when delivery_status='delivered' then failure_reason "
        "when %s in ('failed','undelivered') then %s else failure_reason end,"
        "updated_at=now() where provider_message_id=%s "
        "returning lead_id,outreach_event_id,delivery_status,failure_reason",
        (mapped, mapped, mapped, mapped, mapped, mapped, error, sid),
    )
    sms = sms_result.rowcount
    sms_record = sms_result.fetchone()
    notification = conn.execute(
        "update notification_log set status=case "
        "when %s='delivered' then 'delivered' when status='delivered' then status "
        "when %s in ('failed','undelivered') then %s "
        "when status in ('failed','undelivered') then status "
        "when %s='sent' then 'sent' else status end,"
        "delivered_at=case when %s='delivered' then coalesce(delivered_at,now()) else delivered_at end,"
        "error=case when status='delivered' then error "
        "when %s in ('failed','undelivered') then %s else error end,updated_at=now() "
        "where provider_ref=%s",
        (mapped, mapped, mapped, mapped, mapped, mapped, error, sid),
    ).rowcount
    usage = conn.execute(
        "update test_usage_ledger set status=case "
        "when %s='delivered' then 'delivered' when status='delivered' then status "
        "when %s in ('failed','undelivered') then %s "
        "when status in ('failed','undelivered') then status "
        "when %s='sent' then 'sent' else status end,"
        "finalized_at=case when %s in ('delivered','failed','undelivered') "
        "then coalesce(finalized_at,now()) else finalized_at end "
        "where provider='twilio' and provider_ref=%s",
        (mapped, mapped, mapped, mapped, mapped, sid),
    ).rowcount
    actual_status = sms_record["delivery_status"] if sms_record else None
    if actual_status in {"delivered", "failed", "undelivered"} and sms_record["outreach_event_id"]:
        event = conn.execute(
            "update outreach_events oe set status=case when %s='delivered' then 'delivered' "
            "else 'failed' end,settled_at=coalesce(oe.settled_at,now()),settled_by='webhook',"
            "failure_reason=case when %s='delivered' then oe.failure_reason else %s end,"
            "updated_at=now() where oe.id=%s returning oe.id,oe.lead_id",
            (
                actual_status,
                actual_status,
                sms_record["failure_reason"] or actual_status,
                sms_record["outreach_event_id"],
            ),
        ).fetchone()
        if event:
            enqueue_sheet_update(
                conn,
                lead_id=str(event["lead_id"]),
                event_type="sms_settled",
                source_key=f"{sid}:{actual_status}",
                outreach_event_id=event["id"],
            )
    return sms + notification + usage


def reprocess_failed_twilio_events(trace: WorkflowTrace) -> int:
    settings = get_settings()
    with transaction() as conn:
        rows = conn.execute(
            "select id,payload,processing_attempts from provider_events "
            "where provider='twilio' and event_type='message-status' and processed_at is null "
            "and dead_lettered_at is null and processing_error is not null "
            "and next_attempt_at<=now() and processing_attempts<%s "
            "order by id limit 20 for update skip locked",
            (settings.retry_max_attempts,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "update provider_events set next_attempt_at=now()+interval '5 minutes' where id=%s",
                (row["id"],),
            )
    completed = 0
    for row in rows:
        try:
            with transaction() as conn:
                if not apply_twilio_message_status(conn, row["payload"]):
                    raise LookupError("Twilio message status has no matching local record")
                conn.execute(
                    "update provider_events set processed_at=now(),processing_error=null where id=%s",
                    (row["id"],),
                )
            completed += 1
            trace.log("webhook_reprocessed", provider="twilio", provider_event_id=row["id"])
        except Exception as exc:  # noqa: BLE001 - keep the callback durable until its send row exists
            attempt = row["processing_attempts"] + 1
            delay = retry_delay_seconds(attempt, settings=settings)
            with transaction() as conn:
                conn.execute(
                    "update provider_events set processing_attempts=processing_attempts+1,"
                    "processing_error=%s,next_attempt_at=now()+make_interval(secs=>%s),"
                    "dead_lettered_at=case when processing_attempts+1>=%s then now() else null end "
                    "where id=%s",
                    (str(exc)[:500], delay, settings.retry_max_attempts, row["id"]),
                )
            trace.log(
                "webhook_reprocess_failed",
                provider="twilio",
                provider_event_id=row["id"],
                error_category=type(exc).__name__,
            )
    return completed
