"""Real phone-call tests for the booking voice agent (Elliot). Calls YOUR phone; you play the case.

    uv run --frozen python scripts/voice_tests/call_booking.py cases
    uv run --frozen python scripts/voice_tests/call_booking.py call --to +91XXXXXXXXXX --case 4a
    uv run --frozen python scripts/voice_tests/call_booking.py call --to +91XXXXXXXXXX --case 4e --clinic "Dana Point"
    uv run --frozen python scripts/voice_tests/call_booking.py report <call_id> --case 4a
    uv run --frozen python scripts/voice_tests/call_booking.py compare

`call` creates one test lead (is_test, cadence paused, texts off), has the booking assistant from
config/vapi_booking_tools.json call --to, waits for the end, scores it with the same checks as the chat
benchmark plus timing checks, deletes the test lead and saves results/<time>_case<id>/report.md.
A booking creates real records in the Stride DEMO practice.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts"))
from cases import CASES, CASES_BY_ID, check_case, load_wait_messages

ASSISTANT_ID = json.loads((ROOT / "config" / "vapi_booking_tools.json").read_text())["assistant_id"]
RESULTS = HERE / "results"
CLINICS = ["Laguna Niguel", "Dana Point", "Mission Viejo"]


def vapi(method: str, path: str, **kwargs) -> dict:
    from rpt_agent.config import get_settings

    headers = {"Authorization": f"Bearer {get_settings().vapi_api_key}"}
    for attempt in range(4):
        response = httpx.request(method, f"https://api.vapi.ai{path}", headers=headers, timeout=60, **kwargs)
        if response.status_code < 500:
            if response.status_code >= 400:
                sys.exit(f"Vapi {method} {path} failed: {response.status_code} {response.text[:300]}")
            return response.json()
        time.sleep(2 * (attempt + 1))
    sys.exit(f"Vapi {method} {path} kept failing")


def analyze(call: dict, case) -> dict:
    artifact = call.get("artifact") or {}
    metrics = artifact.get("performanceMetrics") or {}
    turns = [t.get("turnLatency") for t in metrics.get("turnLatencies", []) if t.get("turnLatency")]
    lines, gaps, agent_lines, tools, by_id = [], [], [], [], {}
    user_end = last_agent_end = None
    for message in artifact.get("messages") or []:
        at, role = message.get("secondsFromStart") or 0, message.get("role")
        if role == "user":
            user_end = at + (message.get("duration") or 0) / 1000
            lines.append(f"[{at:6.1f}] CALLER: {message.get('message')}")
        elif role == "bot":
            gap = ""
            if user_end is not None:
                gaps.append(at - user_end)
                gap = f"  (reply gap {at - user_end:.1f}s)"
            lines.append(f"[{at:6.1f}] AGENT : {message.get('message')}{gap}")
            agent_lines.append(message.get("message") or "")
            last_agent_end, user_end = at + (message.get("duration") or 0) / 1000, None
        elif role == "tool_calls":
            for tool_call in message.get("toolCalls", []):
                function = tool_call.get("function", {})
                raw = function.get("arguments") or "{}"
                try:
                    args = json.loads(raw) if isinstance(raw, str) else dict(raw)
                except ValueError:
                    args = {"_raw": raw}
                entry = {"name": function.get("name"), "args": args, "result": "", "after_line": len(agent_lines),
                         "at": at}
                tools.append(entry)
                by_id[tool_call.get("id")] = entry
                lines.append(f"[{at:6.1f}] TOOL  : {entry['name']} {json.dumps(args)[:180]}")
        elif role == "tool_call_result":
            entry = by_id.get(message.get("toolCallId"))
            if entry is not None:
                entry["result"] = str(message.get("result") or "")
                entry["seconds"] = at - entry["at"]
            lines.append(f"[{at:6.1f}] RESULT: {str(message.get('result'))[:180]}")

    duration = None
    if call.get("startedAt") and call.get("endedAt"):
        duration = (datetime.fromisoformat(call["endedAt"])
                    - datetime.fromisoformat(call["startedAt"])).total_seconds()
    checks = check_case(case, agent_lines, tools) if case else []
    avg_gap = sum(gaps) / len(gaps) if gaps else None
    checks.append(("avg reply gap ≤ 1.5s", avg_gap is not None and avg_gap <= 1.5,
                   f"avg {avg_gap:.1f}s, max {max(gaps):.1f}s" if gaps else "no measured turns"))
    tail = (duration - last_agent_end) if duration and last_agent_end else None
    said_bye = any("goodbye" in line.lower() for line in agent_lines)
    checks.append(("hangs up within 6s of goodbye", not said_bye or (tail is not None and tail < 6),
                   f"{tail:.1f}s" if tail is not None else "n/a"))
    intros = sum(1 for line in agent_lines if re.search(r"this is elliot", line.lower()))
    checks.append(("introduces itself once", intros <= 1, f"{intros} intros"))
    return {"ended_reason": call.get("endedReason"), "duration_s": duration, "cost": call.get("cost"),
            "turn_latency_avg_ms": metrics.get("turnLatencyAverage"), "turn_latency_max_ms": max(turns) if turns else None,
            "model_latency_avg_ms": metrics.get("modelLatencyAverage"),
            "voice_latency_avg_ms": metrics.get("voiceLatencyAverage"),
            "reply_gap_avg_s": avg_gap, "reply_gap_max_s": max(gaps) if gaps else None,
            "tool_times": [(t["name"], round(t.get("seconds", 0), 1)) for t in tools], "checks": checks,
            "transcript": lines, "recording": (artifact.get("recording") or {}).get("url") or call.get("recordingUrl")}


def save(call: dict, case_id: str, outcome: dict | None = None) -> tuple[Path, dict]:
    case = CASES_BY_ID.get(case_id)
    tool_ids = (vapi("GET", f"/assistant/{ASSISTANT_ID}").get("model") or {}).get("toolIds", [])
    load_wait_messages(lambda path: vapi("GET", path), tool_ids)
    result = analyze(call, case)
    stamp = (call.get("createdAt") or datetime.now(UTC).isoformat())[:19].replace(":", "-")
    folder = RESULTS / f"{stamp}_case{case_id}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "call.json").write_text(json.dumps(call, indent=1))
    summary = {"call_id": call.get("id"), "case": case_id, "case_name": case.name if case else "?",
               "outcome": outcome, **{k: v for k, v in result.items() if k != "transcript"}}
    (folder / "summary.json").write_text(json.dumps(summary, indent=1, default=str))

    def secs(value):
        return f"{value / 1000:.2f}s" if isinstance(value, (int, float)) else "n/a"

    md = [f"# Booking agent — case {case_id}: {case.name if case else '?'}", "",
          f"- Call ID: `{call.get('id')}`", f"- Ended: {result['ended_reason']} after {result['duration_s'] or 0:.0f}s",
          (f"- Vapi turn latency: avg {secs(result['turn_latency_avg_ms'])}, max {secs(result['turn_latency_max_ms'])} "
           f"(model {secs(result['model_latency_avg_ms'])}, voice {secs(result['voice_latency_avg_ms'])})"),
          f"- Reply gap: avg {result['reply_gap_avg_s'] or 0:.1f}s, max {result['reply_gap_max_s'] or 0:.1f}s",
          "- Tool times: " + (", ".join(f"{n} {s}s" for n, s in result["tool_times"]) or "none"),
          f"- Outcome in our database: {outcome}", f"- Recording: {result['recording'] or 'n/a'}", "",
          f"**Expected:** {case.expect if case else ''}", "", "## Automatic checks"]
    md += [f"- {'PASS' if ok else 'FAIL'} — {name}" + (f" ({detail})" if detail else "")
           for name, ok, detail in result["checks"]]
    md += ["", "## Transcript", "```", *result["transcript"], "```", "", "## Your notes", "- "]
    (folder / "report.md").write_text("\n".join(md))
    return folder, result


def cmd_cases(_args) -> None:
    for case in CASES:
        print(f"\nCASE {case.id}: {case.name}")
        for line in case.you_say:
            print(f"   you: {line}")
        print(f"   expect: {case.expect}")


def cmd_call(args) -> None:
    case = CASES_BY_ID.get(args.case) or sys.exit(f"Unknown case {args.case}. Run: call_booking.py cases")
    if not re.fullmatch(r"\+\d{8,15}", args.to):
        sys.exit("--to must be E.164, e.g. +919820000000")
    from test_booking_call import create_test_lead, delete_test_lead

    from rpt_agent.db import transaction
    from rpt_agent.services.slot_booking import call_case_variables

    lead_args = argparse.Namespace(name=args.name, phone=args.to, dob=args.dob, clinic=args.clinic,
                                   case=args.service)
    lead_id, event_id, phone_number_id = create_test_lead(transaction, lead_args)
    with transaction() as conn:
        case_variables = call_case_variables(conn, lead_id)
    print(f"\nCASE {case.id}: {case.name}")
    for line in case.you_say:
        print(f"   you: {line}")
    print(f"   expect: {case.expect}\n")
    outcome = None
    try:
        call = vapi("POST", "/call", json={
            "assistantId": ASSISTANT_ID, "phoneNumberId": phone_number_id,
            "customer": {"number": args.to}, "name": f"TEST booking case {case.id}",
            "assistantOverrides": {"variableValues": {
                "lead_id": lead_id, "outreach_event_id": str(event_id), "Patient_name": args.name,
                "patient_name": args.name, "call_attempt": "1", "clinic_location": args.clinic,
                "case_name": args.service, **case_variables}}})
        with transaction() as conn:
            conn.execute("update outreach_events set status='attempted',executed_at=now(),provider='vapi',"
                         "provider_ref=%s,vapi_call_id=%s where id=%s", (call["id"], call["id"], event_id))
        print(f"Calling {args.to} … call id {call['id']}. Answer the phone.")
        for _ in range(300):
            if call.get("status") == "ended":
                break
            time.sleep(4)
            call = vapi("GET", f"/call/{call['id']}")
        for _ in range(10):  # the transcript arrives a few seconds after the end
            if (call.get("artifact") or {}).get("messages"):
                break
            time.sleep(3)
            call = vapi("GET", f"/call/{call['id']}")
        time.sleep(6)  # let the end-of-call report reach our server
        with transaction() as conn:
            lead = conn.execute("select status,callback_requested_at from leads where id=%s", (lead_id,)).fetchone()
            appointment = conn.execute("select state,stride_appointment_id,start_utc from appointments "
                                       "where lead_id=%s order by id desc limit 1", (lead_id,)).fetchone()
        outcome = {"lead_status": lead and lead["status"],
                   "callback_at": str(lead["callback_requested_at"]) if lead and lead["callback_requested_at"] else None,
                   "appointment": {k: str(v) for k, v in appointment.items()} if appointment else None}
        folder, result = save(call, case.id, outcome)
        print(f"\nEnded: {result['ended_reason']} | avg reply gap {result['reply_gap_avg_s'] or 0:.1f}s | {outcome}")
        for name, ok, detail in result["checks"]:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" ({detail})" if detail and not ok else ""))
        print(f"\nSaved → {folder}/report.md")
    finally:
        if args.keep:
            print(f"Kept test lead {lead_id}.")
        else:
            delete_test_lead(transaction, lead_id)


def cmd_report(args) -> None:
    folder, _ = save(vapi("GET", f"/call/{args.call_id}"), args.case)
    print(f"Saved → {folder}/report.md")


def cmd_compare(_args) -> None:
    rows = sorted(RESULTS.glob("*_case*/summary.json"))
    if not rows:
        print("No saved calls yet.")
        return
    print(f"{'run':34} {'case':5} {'ended':28} {'gap avg':>8} {'turn avg':>9} checks")
    for path in rows:
        s = json.loads(path.read_text())
        passed = sum(1 for check in s["checks"] if check[1])
        print(f"{path.parent.name[:34]:34} {s['case']:5} {str(s['ended_reason'])[:28]:28} "
              f"{(s['reply_gap_avg_s'] or 0):7.1f}s {(s.get('turn_latency_avg_ms') or 0) / 1000:8.2f}s "
              f"{passed}/{len(s['checks'])}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("cases").set_defaults(fn=cmd_cases)
    call = sub.add_parser("call")
    call.add_argument("--to", required=True, help="your phone in E.164, e.g. +919820000000")
    call.add_argument("--case", default="4a")
    call.add_argument("--name", default="Rudraksh Mehta", help="patient name the agent asks for")
    call.add_argument("--dob", default="1995-01-01")
    call.add_argument("--clinic", default="Laguna Niguel", choices=CLINICS)
    call.add_argument("--service", default="Physical Therapy")
    call.add_argument("--keep", action="store_true", help="keep the test lead afterwards")
    call.set_defaults(fn=cmd_call)
    report = sub.add_parser("report")
    report.add_argument("call_id")
    report.add_argument("--case", default="4a")
    report.set_defaults(fn=cmd_report)
    sub.add_parser("compare").set_defaults(fn=cmd_compare)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
