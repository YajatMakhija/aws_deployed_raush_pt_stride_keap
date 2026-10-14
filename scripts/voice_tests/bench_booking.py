"""Chat benchmark for the booking voice agent: a simulated patient talks to the agent (no phone calls).

    uv run --frozen python scripts/voice_tests/bench_booking.py                      # all cases, 3 models
    uv run --frozen python scripts/voice_tests/bench_booking.py --models gpt-5.6-terra --cases 4a 4b
    uv run --frozen python scripts/voice_tests/bench_booking.py --list

How it works, per case and model:
1. Creates one test lead in the database from .env (is_test, cadence paused, texts off).
2. Runs the agent through Vapi /chat as a transient copy of the booking assistant with the prompt in
   config/vapi_booking_assistant_prompt.md and the chosen model. The real tools run, so a booking
   creates real records in the Stride DEMO practice and marks the test lead booked.
3. A second /chat "patient" (gpt-5.6-terra, no tools) plays the case's persona.
4. Scores the conversation with cases.check_case, deletes the test lead, saves everything to
   scripts/voice_tests/results/<time>/.

The lead-status tool needs a real call id, so in chat it answers with an error; the check only looks at
whether the agent called it with the right values. The saved Vapi assistant is never changed.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
from cases import CASES, CASES_BY_ID, PATIENT_FIRST, TRANSFER, check_case, load_wait_messages

TOOLS_CONFIG = json.loads((ROOT / "config" / "vapi_booking_tools.json").read_text())
ASSISTANT_ID = TOOLS_CONFIG["assistant_id"]
PROMPT = (ROOT / "config" / "vapi_booking_assistant_prompt.md").read_text()
REASONING_EFFORT = os.environ.get("BENCH_REASONING_EFFORT", "")  # e.g. low; empty = model default
DEFAULT_MODELS = ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]
PATIENT_MODEL = "gpt-5.6-terra"
MAX_TURNS = 28
VAPI = "https://api.vapi.ai"
_print_lock = threading.Lock()


def log(*parts) -> None:
    with _print_lock:
        print(*parts, flush=True)


class Vapi:
    def __init__(self, key: str):
        self.client = httpx.Client(base_url=VAPI, headers={"Authorization": f"Bearer {key}"}, timeout=90)

    def request(self, method: str, path: str, **kwargs) -> dict:
        for attempt in range(4):
            response = self.client.request(method, path, **kwargs)
            if response.status_code < 500 and response.status_code != 429:
                if response.status_code >= 400:
                    raise RuntimeError(f"Vapi {method} {path}: {response.status_code} {response.text[:300]}")
                return response.json()
            time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"Vapi {method} {path} kept failing: {response.status_code}")


def agent_assistant(base: dict, model: str) -> dict:
    """Transient copy of the saved booking assistant with our prompt and the chosen model."""
    source = base["model"]
    return {
        "name": f"bench {model}",
        "model": {"provider": source.get("provider", "openai"), "model": model,
                  **({"reasoningEffort": REASONING_EFFORT} if REASONING_EFFORT else {}),
                  "messages": [{"role": "system", "content": PROMPT}],
                  "toolIds": source.get("toolIds", [])},
        "server": base.get("server"),
    }


def patient_assistant(case, last_name: str, dob_words: str) -> dict:
    spelled = " ".join(last_name.upper())
    system = (
        f"You are role-playing a patient called {PATIENT_FIRST} {last_name} who receives a phone call from "
        f"Rausch Physical Therapy. Your last name is spelled {spelled}. Your date of birth is {dob_words}.\n"
        f"How to behave in this call: {case.persona}\n"
        "Speak like a real person on the phone: one short sentence, plain words, no stage directions. "
        "Reply only with what you say out loud. Never play the clinic's side. If the clinic agent says "
        "goodbye, transfers you, or the call is clearly over, reply exactly [HANG UP]."
    )
    return {"name": "bench patient", "model": {"provider": "openai", "model": PATIENT_MODEL,
                                               "messages": [{"role": "system", "content": system}]}}


def parse_output(output: list[dict]) -> tuple[list[str], list[dict]]:
    """Agent lines and tool calls (with results) from one /chat reply."""
    lines, calls, by_id = [], [], {}
    for message in output or []:
        role = message.get("role")
        for tool_call in message.get("tool_calls") or message.get("toolCalls") or []:
            function = tool_call.get("function", {})
            raw = function.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except ValueError:
                args = {"_raw": raw}
            entry = {"name": function.get("name"), "args": args, "result": ""}
            calls.append(entry)
            by_id[tool_call.get("id")] = entry
        if role == "tool":
            target = by_id.get(message.get("tool_call_id") or message.get("toolCallId"))
            if target is not None:
                target["result"] = str(message.get("content") or "")
        elif role == "assistant" and (message.get("content") or "").strip():
            lines.append(message["content"].strip())
    return lines, calls


def make_last_name() -> str:
    consonants, vowels = "bdfgklmnprstvz", "aeiou"
    return "".join(random.choice(consonants if i % 2 == 0 else vowels) for i in range(6)).capitalize()


def run_case(vapi: Vapi, base: dict, model: str, case, variables_for, cleanup) -> dict:
    last_name = make_last_name()
    dob = f"{random.randint(1965, 2001)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"
    dob_words = datetime.strptime(dob, "%Y-%m-%d").strftime("%B %-d, %Y")
    case.patient_dob = dob
    # The Sheet's birthday: the patient's own, or a wrong one (ten days later) for the correction case.
    on_file = (datetime.strptime(dob, "%Y-%m-%d") + timedelta(days=10)).strftime("%Y-%m-%d") \
        if case.wrong_dob_on_file else dob
    lead_id, variables = variables_for(f"{PATIENT_FIRST} {last_name}", on_file, case.sheet_case)
    agent, patient = agent_assistant(base, model), patient_assistant(case, last_name, dob_words)
    opener = f"Hi, am I speaking with {PATIENT_FIRST} {last_name}?"
    transcript, agent_lines, tools, latencies = [("AGENT", opener)], [opener], [], []
    agent_chat = patient_chat = None
    try:
        reply = vapi.request("POST", "/chat", json={"assistant": patient, "input": f"The phone rings. You answer and hear: \"{opener}\""})
        patient_chat = reply["id"]
        for _turn in range(MAX_TURNS):
            said = " ".join(m.get("content") or "" for m in reply.get("output", []) if m.get("role") == "assistant").strip()
            if not said or "[HANG UP]" in said:
                transcript.append(("PATIENT", "[hangs up]"))
                break
            transcript.append(("PATIENT", said))
            body = {"assistant": agent, "assistantOverrides": {"variableValues": variables}}
            if agent_chat:
                body |= {"previousChatId": agent_chat, "input": said}
            else:
                body |= {"input": [{"role": "assistant", "content": opener}, {"role": "user", "content": said}]}
            started = time.time()
            answer = vapi.request("POST", "/chat", json=body)
            latencies.append(time.time() - started)
            agent_chat = answer["id"]
            lines, calls = parse_output(answer.get("output", []))
            for call in calls:
                call["after_line"] = len(agent_lines)
                tools.append(call)
                transcript.append(("TOOL", f"{call['name']} {json.dumps(call['args'])[:220]}"))
                transcript.append(("RESULT", call["result"][:220]))
            for line in lines:
                agent_lines.append(line)
                transcript.append(("AGENT", line))
            if any(c["name"] == TRANSFER for c in calls) or (lines and "goodbye" in lines[-1].lower()):
                break
            reply = vapi.request("POST", "/chat", json={
                "assistant": patient, "previousChatId": patient_chat,
                "input": " ".join(lines) or "(silence)"})
            patient_chat = reply["id"]
        error = None
    except Exception as exc:  # keep the run going; the case fails with the reason
        error = str(exc)[:300]
    finally:
        outcome = cleanup(lead_id)
    checks = check_case(case, agent_lines, tools)
    if error:
        checks.insert(0, ("conversation ran", False, error))
    return {"model": model, "case": case.id, "case_name": case.name, "checks": checks,
            "passed": sum(ok for _, ok, _ in checks), "total": len(checks),
            "turns": len(agent_lines), "latency_avg": sum(latencies) / len(latencies) if latencies else None,
            "latency_max": max(latencies) if latencies else None, "outcome": outcome,
            "transcript": transcript, "expect": case.expect, "last_name": last_name}


def db_helpers(keep: bool):
    # One plain connection, not the app's pool: the Supabase session pooler allows 15 clients and the
    # live server needs them (a pool here hit "max clients reached" on the first run).
    from contextlib import contextmanager

    import psycopg
    from psycopg.rows import dict_row

    from rpt_agent.config import get_settings
    from rpt_agent.services.slot_booking import call_case_variables

    conn_lock = threading.Lock()
    shared = psycopg.connect(get_settings().supabase_db_url, row_factory=dict_row)

    @contextmanager
    def transaction():
        with conn_lock, shared.transaction():
            yield shared

    def variables_for(name: str, dob: str, sheet_case: str = "Physical Therapy") -> tuple[str, dict]:
        first, _, last = name.partition(" ")
        with transaction() as conn:
            practice = conn.execute("select id from practices where slug='rausch-pt'").fetchone()["id"]
            lead = str(conn.execute(
                "insert into leads(practice_id,source_system,full_name,first_name,last_name,phone_e164,"
                "date_of_birth,status,cadence_state,is_test,sms_opt_out,location,lead_type,status_reason) "
                "values(%s,'dashboard',%s,%s,%s,%s,%s,'in_progress','paused',true,true,'Laguna Niguel',"
                "%s,'booking agent chat benchmark') returning id",
                (practice, name, first, last, f"+1555{random.randint(1000000, 9999999)}", dob, sheet_case),
            ).fetchone()["id"])
            case_variables = call_case_variables(conn, lead)
        return lead, {"lead_id": lead, "Patient_name": name, "patient_name": name, "call_attempt": "1",
                      "clinic_location": "Laguna Niguel", "case_name": sheet_case, "date_of_birth": dob,
                      "date_of_birth_spoken": datetime.strptime(dob, "%Y-%m-%d").strftime("%B %-d, %Y"),
                      **case_variables}

    def cleanup(lead_id: str) -> dict:
        with transaction() as conn:
            lead = conn.execute("select status from leads where id=%s", (lead_id,)).fetchone()
            appointment = conn.execute(
                "select state,stride_appointment_id from appointments where lead_id=%s order by id desc limit 1",
                (lead_id,)).fetchone()
            outcome = {"lead_status": lead and lead["status"],
                       "appointment": dict(appointment) if appointment else None}
            if not keep:
                for table in ("appointments", "dashboard_sms_requests", "sms_messages", "notification_log",
                              "outreach_events", "slot_offers", "slot_holds"):
                    conn.execute(f"delete from {table} where lead_id=%s", (lead_id,))
                conn.execute("delete from integration_outbox where aggregate_id=%s", (lead_id,))
                conn.execute("delete from leads where id=%s and is_test", (lead_id,))
        return outcome

    return variables_for, cleanup


def write_results(runs: list[dict], models: list[str], folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "runs.json").write_text(json.dumps(runs, indent=1, default=str))
    md = [f"# Booking agent benchmark {datetime.now():%Y-%m-%d %H:%M}", "",
          "| Case | " + " | ".join(models) + " |", "|---|" + "---|" * len(models)]
    for case in CASES:
        cells = []
        for model in models:
            run = next((r for r in runs if r["model"] == model and r["case"] == case.id), None)
            if run:
                mark = "✅" if run["passed"] == run["total"] else "❌"
                cells.append(f"{mark} {run['passed']}/{run['total']}, {run['latency_avg'] or 0:.1f}s")
        if any(cells):
            md.append(f"| {case.id} {case.name} | " + " | ".join(cells) + " |")
    md.append("")
    for model in models:
        mine = [r for r in runs if r["model"] == model]
        if mine:
            full = sum(r["passed"] == r["total"] for r in mine)
            lat = [r["latency_avg"] for r in mine if r["latency_avg"]]
            md.append(f"- **{model}**: {full}/{len(mine)} cases fully passed, "
                      f"avg reply {sum(lat) / len(lat) if lat else 0:.1f}s (chat time incl. tools)")
    md.append("")
    for run in runs:
        md += [f"## {run['model']} — case {run['case']}: {run['case_name']}", "", f"**Expected:** {run['expect']}", "",
               f"Outcome: {run['outcome']}", ""]
        md += [f"- {'PASS' if ok else 'FAIL'} {name}" + (f" ({detail})" if detail and not ok else "")
               for name, ok, detail in run["checks"]]
        md += ["", "```"] + [f"{who:7} {text}" for who, text in run["transcript"]] + ["```", ""]
    path = folder / "report.md"
    path.write_text("\n".join(md))
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    parser.add_argument("--cases", nargs="+", help="case ids, e.g. 1 4a 4e (default: all)")
    parser.add_argument("--keep", action="store_true", help="keep the test leads")
    parser.add_argument("--list", action="store_true", help="list the cases and exit")
    args = parser.parse_args()
    if args.list:
        for case in CASES:
            print(f"{case.id:3} {case.name}\n    expect: {case.expect}")
        return
    cases = [CASES_BY_ID[c] for c in args.cases] if args.cases else CASES

    from rpt_agent.config import get_settings

    vapi = Vapi(get_settings().vapi_api_key)
    base = vapi.request("GET", f"/assistant/{ASSISTANT_ID}")
    load_wait_messages(lambda path: vapi.request("GET", path), base["model"].get("toolIds", []))
    variables_for, cleanup = db_helpers(args.keep)
    runs: list[dict] = []

    def run_model(model: str) -> None:
        for case in cases:
            log(f"[{model}] case {case.id} {case.name} …")
            run = run_case(vapi, base, model, case, variables_for, cleanup)
            runs.append(run)
            failed = [name for name, ok, _ in run["checks"] if not ok]
            log(f"[{model}] case {case.id}: {run['passed']}/{run['total']}"
                + (f"  FAILED: {', '.join(failed)}" if failed else "  all checks passed"))

    with ThreadPoolExecutor(max_workers=len(args.models)) as pool:
        list(pool.map(run_model, args.models))
    folder = HERE / "results" / datetime.now().strftime("%Y-%m-%dT%H-%M")
    path = write_results(runs, args.models, folder)
    print("\nSummary:")
    for model in args.models:
        mine = [r for r in runs if r["model"] == model]
        lat = [r["latency_avg"] for r in mine if r["latency_avg"]]
        print(f"  {model:15} {sum(r['passed'] == r['total'] for r in mine)}/{len(mine)} cases fully passed, "
              f"avg reply {sum(lat) / len(lat) if lat else 0:.1f}s")
    print(f"Report: {path}")


if __name__ == "__main__":
    main()
