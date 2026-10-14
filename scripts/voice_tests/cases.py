"""Booking voice agent test cases, shared by the chat benchmark and the phone-call runner.

Each case has:
- `persona`: instructions for the simulated patient in the chat benchmark (bench_booking.py).
- `you_say`: what a human says when testing the same case on a real phone call (call_booking.py).
- `expect`: what the agent should do, in plain words (printed and saved with every run).
- `checks`: machine checks run on the transcript and tool calls (see `check_case`).

Statuses are the lead-status tool's values. Tool names are the Vapi function names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

LEAD_STATUS, FIND, BOOK, PROVIDERS, TRANSFER = (
    "update_lead_status", "find_slots", "book_appointment", "list_providers", "transfer_tool_outreach")

# The simulated patient's name, birthday and spelling. The last name is made unique per run
# (Stride refuses a second patient with the same name + birthday).
PATIENT_FIRST = "Rudraksh"


@dataclass
class Case:
    id: str
    name: str
    persona: str
    you_say: list[str]
    expect: str
    status: str | None = None          # lead-status value that must be reported (None = none)
    must_call: list[str] = field(default_factory=list)
    must_not_call: list[str] = field(default_factory=list)
    min_find: int = 0                  # at least this many open-times searches
    booked: bool | None = None         # True = booking tool must answer BOOKED; False = no booking
    asks_before_transfer: bool = False  # transfer must come after the agent offered it
    must_say: list[str] = field(default_factory=list)      # regex, any agent line
    must_not_say: list[str] = field(default_factory=list)  # regex, no agent line
    sheet_case: str = "Physical Therapy"  # the lead's Case column (sent as case_name)
    wrong_dob_on_file: bool = False       # the Sheet's birthday differs from the patient's
    booked_case: str | None = None        # case_type the booking must use (absent = the one on file)
    books_with_patient_dob: bool = False  # booking must send the patient's own birthday


BOOKING_BASE = (
    "When asked to spell your last name, spell it letter by letter with spaces. When asked your date "
    "of birth, give it in words. Confirm the spelling when it is read back correctly."
)

CASES: list[Case] = [
    Case(
        "8d", "Misheard clinic ('data point') and a muddled birthday",
        "Confirm you are the patient and that now is fine. When told your clinic, say: can I change that to "
        "data point? (you mean Dana Point, but say 'data point'). Keep the service. Anyone is fine. When your "
        "date of birth is read back and it is right, say: yes that's correct. Pick the first time offered. "
        "When asked to spell your last name, spell it letter by letter.",
        ["'Yes.'", "'Sure.'", "'Can I change that to data point?'", "'Yes.'", "'Anyone.'", "(spell)", "'Yes.'", "'First one.'"],
        "Takes 'data point' as Dana Point and says Dana Point back, never speaks notes or reasoning, books "
        "at Dana Point.",
        must_call=[FIND, BOOK], booked=True, must_say=[r"dana point"], must_not_say=[r"data point"],
    ),
    Case(
        "8a", "Referral says Ortho: confirm in its words, birthday read back",
        "Confirm you are the patient and that now is fine. Keep the clinic. When asked if the visit is for "
        "ortho, say yes. Any therapist. When your date of birth is read back and it is right, say yes. Pick "
        "the first time offered. " + "When asked to spell your last name, spell it letter by letter.",
        ["'Yes.'", "'Sure.'", "'Yes.'", "'Yes, ortho.'", "'Anyone.'", "(spell)", "'Yes, that's right.'", "'The first one.'"],
        "Says 'this visit is for ortho', never offers the pelvic service, reads the birthday on file back "
        "instead of asking for it, books Physical Therapy.",
        must_call=[FIND, BOOK], booked=True, sheet_case="Ortho", booked_case="Physical Therapy",
        must_say=[r"\bortho", r"date of birth (as|is)"], must_not_say=[r"pelvic", r"what is your date of birth"],
    ),
    Case(
        "8b", "Patient says 'it's for ortho' when asked about Physical Therapy",
        "Confirm you are the patient and that now is fine. Keep the clinic. When asked what the visit is for, "
        "say: no, it's for ortho. Any therapist. Confirm your date of birth if it is right. Pick the first "
        "time offered. When asked to spell your last name, spell it letter by letter.",
        ["'Yes.'", "'Sure.'", "'Yes.'", "'No, it's for ortho.'", "'Anyone.'", "(spell)", "'Yes.'", "'First one.'"],
        "Understands ortho = Physical Therapy without asking the patient to choose between services; books "
        "Physical Therapy.",
        must_call=[FIND, BOOK], booked=True, sheet_case="Physical Therapy", booked_case="Physical Therapy",
        must_say=[r"ortho.*physical therapy|physical therapy.*ortho"], must_not_say=[r"pelvic"],
    ),
    Case(
        "8c", "Birthday on file is wrong: patient corrects it",
        "Confirm you are the patient and that now is fine. Keep the clinic and the service. Any therapist. "
        "When your date of birth is read back, it is WRONG: say no, and give your real date of birth. Pick "
        "the first time offered. When asked to spell your last name, spell it letter by letter.",
        ["'Yes.'", "'Sure.'", "'Yes.'", "'Yes.'", "'Anyone.'", "(spell)", "'No, it's <your real date>.'", "'First one.'"],
        "Reads the wrong birthday back, accepts the correction without fuss, books with the patient's date "
        "(staff see a 'check date of birth' note on the dashboard and in the Sheet).",
        must_call=[FIND, BOOK], booked=True, wrong_dob_on_file=True, books_with_patient_dob=True,
        must_say=[r"date of birth (as|is)"],
    ),
    Case(
        "1", "Busy → callback in 20 minutes",
        "You are busy at work. Confirm you are the patient. When asked if now is a good time, say you are "
        "busy and ask them to call you back in twenty minutes. Accept the confirmation.",
        ["'Yes, speaking.'", "'I'm at work right now, can you call me back in twenty minutes?'",
         "'Yes, that works.'"],
        "Offers callback or text link, confirms 'in twenty minutes' once, reports callback_scheduled "
        "(relative, 20 minutes), says goodbye. No booking questions.",
        status="callback_scheduled", must_not_call=[FIND, BOOK, TRANSFER], booked=False,
        must_say=[r"twenty minutes|20 minutes"],
    ),
    Case(
        "1b", "Busy → callback tomorrow at 3 PM",
        "You are driving. Confirm you are the patient. Say you're driving and ask to be called tomorrow "
        "at three in the afternoon. If they propose another time because of their hours, accept it.",
        ["'Yeah, it's me.'", "'I'm driving, can you call me tomorrow at 3 PM?'", "'Yes.'"],
        "Confirms tomorrow at three (or the next weekday if tomorrow is a weekend), reports "
        "callback_scheduled (absolute), goodbye.",
        status="callback_scheduled", must_not_call=[FIND, BOOK], booked=False,
    ),
    Case(
        "2", "Busy → text me the booking link",
        "Confirm you are the patient. Say now is not a good time. When offered a callback or a text link, "
        "choose the text link and confirm it.",
        ["'Yes.'", "'Not really, I'm busy.'", "'Just text me the link.'", "'Yes.'"],
        "Offers callback or link, confirms the text once, reports booking_link, goodbye.",
        status="booking_link", must_not_call=[FIND, BOOK, TRANSFER], booked=False,
        must_say=[r"\blink\b"],
    ),
    Case(
        "3", "Asks for a human right away",
        "Confirm you are the patient. When asked if now is a good time, say: can I just talk to a real "
        "person at the front desk?",
        ["'Yes.'", "'Can I just talk to a real person?'"],
        "Transfers at once: reports transferred_human and calls the transfer tool, no questions first.",
        status="transferred_human", must_call=[TRANSFER], must_not_call=[FIND, BOOK], booked=False,
    ),
    Case(
        "4a", "Happy path: book the first time offered",
        "You want to book. Confirm the clinic and the service, say anyone is fine as therapist, spell your "
        "last name, give your birthday, then pick the first time offered. " + BOOKING_BASE,
        ["'Yes.' → 'Sure.' → 'Yes' (clinic) → 'Yes' (service) → 'Anyone is fine.'",
         "Spell your last name → 'Yes' → give your birthday",
         "Pick the first time offered"],
        "Asks clinic, service, therapist, spelling, birthday; finds times; books; confirms day, date, "
        "time, therapist, clinic; mentions photo ID; goodbye.",
        must_call=[FIND, BOOK], must_not_call=[TRANSFER], booked=True, status=None,
        must_say=[r"all set", r"photo id"],
    ),
    Case(
        "4b", "Insurance question → agrees to transfer",
        "You want to book but first you care about insurance. After confirming identity and that now is a "
        "good time, ask: do you take Blue Shield PPO? When offered a transfer to the front desk, say yes "
        "please.",
        ["'Yes.' → 'Sure.'", "'Do you take Blue Shield PPO?'", "'Yes, please transfer me.'"],
        "Does not answer the insurance question itself, explains the front desk can help and ASKS before "
        "transferring; after yes, reports transferred_human and transfers.",
        status="transferred_human", must_call=[TRANSFER], must_not_call=[BOOK], booked=False,
        asks_before_transfer=True, must_say=[r"front desk"],
        must_not_say=[r"we (do )?(take|accept) blue shield", r"in[- ]network"],
    ),
    Case(
        "4c", "Insurance question → declines transfer, books anyway",
        "You want to book. After confirming the clinic, ask: is this covered by my insurance? How much "
        "will it cost? When offered a transfer, say no thanks, let's just book. Then continue: anyone is "
        "fine, spell your last name, give your birthday, pick the first time. " + BOOKING_BASE,
        ["'Yes.' → 'Sure.' → 'Yes' (clinic)", "'Is this covered by my insurance? How much will it cost?'",
         "'No thanks, let's just book.' → finish booking"],
        "Offers the front desk for insurance/cost, accepts the no, continues booking where it was, books.",
        must_call=[FIND, BOOK], must_not_call=[TRANSFER], booked=True,
        must_say=[r"front desk"], must_not_say=[r"\$\s?\d", r"dollars"],
    ),
    Case(
        "4d", "Many physical-therapy questions",
        "You are curious and a bit anxious. After confirming identity and that now is a good time, ask "
        "these one at a time before booking: what happens at the first visit? will physical therapy fix "
        "my lower back pain? how many sessions will I need? what are your Saturday hours? Then agree to "
        "book: anyone is fine, spell your last name, give your birthday, pick the first time. "
        + BOOKING_BASE,
        ["'Yes.' → 'Sure.'", "'What happens at the first visit?'", "'Will it fix my lower back pain?'",
         "'How many sessions will I need?'", "'What are your Saturday hours?'", "Then book."],
        "Short answers from clinic facts only, no medical promises ('your therapist will go over that'), "
        "after several questions offers to book now or transfer, then books.",
        must_call=[FIND, BOOK], booked=True,
        must_say=[r"evaluation|therapist will"],
        must_not_say=[r"will (definitely )?fix", r"\b\d+ (to \d+ )?sessions\b", r"guarantee"],
    ),
    Case(
        "4e", "Changes the day several times",
        "You want to book but you are picky about the day. Confirm clinic, service, anyone is fine, spell "
        "your last name, give birthday. When times are offered, say they don't work and ask for next week "
        "instead. Then ask for a morning. Then ask for Thursday afternoon. Then pick the first time offered. "
        + BOOKING_BASE,
        ["Book normally until times are offered", "'That doesn't work, anything next week?'",
         "'Something in the morning?'", "'What about Thursday afternoon?'", "Pick one"],
        "Calls the open-times tool again for each change (3+ searches), never invents a time, never "
        "repeats an old list, then books.",
        must_call=[FIND, BOOK], min_find=3, booked=True,
    ),
    Case(
        "4f", "Asks about the therapists, picks one",
        "You want to book. When asked about a therapist, ask: who are your therapists? Then ask a little "
        "about one of them, then choose that therapist. Then spell your last name, give birthday, pick the "
        "first time offered. " + BOOKING_BASE,
        ["Book normally until the therapist question", "'Who are your therapists?'",
         "'Tell me about [one of them].'", "'I'll go with [them].' → finish"],
        "Calls the provider tool once, names the therapists, answers about the one asked, books with that "
        "therapist (or explains if they have no openings).",
        must_call=[PROVIDERS, FIND, BOOK], booked=True,
    ),
    Case(
        "4g", "Wants a different clinic",
        "You want to book but at Dana Point instead of the clinic on file. Say anyone is fine, spell your "
        "last name, give birthday, pick the first time offered. " + BOOKING_BASE,
        ["'Yes.' → 'Sure.'", "'Actually, Dana Point is closer for me.'", "Finish booking"],
        "Accepts Dana Point, sends it as location to the tools, confirms the booking at Dana Point.",
        must_call=[FIND, BOOK], booked=True, must_say=[r"dana point"],
    ),
    Case(
        "5", "Not interested",
        "Confirm you are the patient. Say you're not interested and already found another clinic.",
        ["'Yes.'", "'I'm not interested, I already found another clinic.'"],
        "Reports declined, short polite goodbye, no pushing.",
        status="declined", must_not_call=[FIND, BOOK, TRANSFER], booked=False,
    ),
    Case(
        "6", "Wrong person",
        "You are the patient's roommate, not the patient. Say: no, this is their roommate, they're not "
        "here.",
        ["'No, this is their roommate. They're not here.'"],
        "Reports wrong_person, does NOT mention physical therapy, the clinic or a referral, goodbye.",
        status="wrong_person", must_not_call=[FIND, BOOK, TRANSFER], booked=False,
        must_not_say=[r"physical therapy|referral|evaluation"],
    ),
    Case(
        "7", "Do not call me again",
        "Confirm you are the patient. Say firmly: please stop calling me and take me off your list.",
        ["'Yes.'", "'Please stop calling me, take me off your list.'"],
        "Reports do_not_contact, short goodbye.",
        status="do_not_contact", must_not_call=[FIND, BOOK, TRANSFER], booked=False,
    ),
]
CASES_BY_ID = {case.id: case for case in CASES}

# Checks that apply to every case.
GENERIC_FORBIDDEN = [r"\bsarah\b", r"one moment", r"let me check", r"bear with me", r"just a sec",
                     # reasoning spoken aloud (demo call 2026-10-14: "We need answer typo Dana point ...")
                     r"\btypo\b", r"\bensure\b", r"should confirm", r"must know", r"next service"]


# Tools speak their own waiting message ("One moment, please, while I check our availability.") when
# they start. It is not the model talking, so it is left out of the checks.
TOOL_WAIT_MESSAGES: set[str] = set()


def load_wait_messages(get, tool_ids: list[str]) -> None:
    """Fill TOOL_WAIT_MESSAGES from the tools' request-start messages. `get(path)` returns Vapi JSON."""
    for tool_id in tool_ids:
        for message in get(f"/tool/{tool_id}").get("messages") or []:
            if message.get("type") == "request-start" and message.get("content"):
                TOOL_WAIT_MESSAGES.add(message["content"].strip())


def model_lines(agent_lines: list[str]) -> list[str]:
    return [line for line in agent_lines if line.strip() not in TOOL_WAIT_MESSAGES]


def check_case(case: Case, agent_lines: list[str], tools: list[dict]) -> list[tuple[str, bool, str]]:
    """Return (check, passed, detail) for one finished conversation.

    `tools` = [{"name", "args" (dict), "result" (str), "after_line" (index of agent lines so far)}].
    """
    agent_lines = model_lines(agent_lines)
    names = [tool["name"] for tool in tools]
    statuses = [tool["args"].get("status") for tool in tools if tool["name"] == LEAD_STATUS]
    text = [line.lower() for line in agent_lines]
    checks: list[tuple[str, bool, str]] = []

    for name in case.must_call:
        checks.append((f"calls {name}", name in names, f"tools: {names}"))
    for name in case.must_not_call:
        checks.append((f"does not call {name}", name not in names, f"tools: {names}"))
    if case.status:
        checks.append((f"reports {case.status}", case.status in statuses, f"statuses: {statuses}"))
        checks.append(("reports only once", statuses.count(case.status) == 1, f"statuses: {statuses}"))
    elif case.booked:
        checks.append(("no lead-status report on a booking", not statuses, f"statuses: {statuses}"))
    if case.status == "callback_scheduled":
        args = next((t["args"] for t in tools if t["args"].get("status") == "callback_scheduled"), {})
        ok = (args.get("callback_type") == "relative" and str(args.get("delay_minutes")) not in ("", "None")) or (
            args.get("callback_type") == "absolute" and bool(args.get("callback_datetime_iso")))
        checks.append(("callback time sent", ok, str({k: args.get(k) for k in (
            "callback_type", "delay_minutes", "callback_datetime_iso")})))
    if case.min_find:
        found = names.count(FIND)
        checks.append((f"searches times {case.min_find}+ times", found >= case.min_find, f"{found} searches"))
    if case.booked is not None:
        booked = any(t["name"] == BOOK and str(t["result"]).upper().startswith(("BOOKED", "ALREADY_BOOKED"))
                     for t in tools)
        checks.append(("booked" if case.booked else "no booking", booked == case.booked,
                       "; ".join(str(t["result"])[:80] for t in tools if t["name"] == BOOK) or "no booking call"))
    if case.asks_before_transfer:
        at = next((t["after_line"] for t in tools if t["name"] == TRANSFER), None)
        offered = at is not None and any(re.search(r"transfer|connect", line) for line in text[:at])
        checks.append(("offers before transferring", offered, f"transfer after agent line {at}"))
    booking = next((t["args"] for t in tools if t["name"] == BOOK), None)
    if case.booked_case and booking is not None:
        used = booking.get("case_type") or case.booked_case  # absent = the service on file
        checks.append((f"books {case.booked_case}", used == case.booked_case, f"case_type: {used}"))
    if case.books_with_patient_dob and booking is not None:
        checks.append(("books with the patient's birthday", booking.get("date_of_birth") == case.patient_dob,
                       f"sent {booking.get('date_of_birth')}, patient {case.patient_dob}"))
    for pattern in case.must_say:
        checks.append((f"says /{pattern}/", any(re.search(pattern, line) for line in text), ""))
    for pattern in case.must_not_say + GENERIC_FORBIDDEN:
        hits = [line for line in text if re.search(pattern, line)]
        checks.append((f"never says /{pattern}/", not hits, hits[0][:90] if hits else ""))

    multi_q = [line for line in agent_lines if line.count("?") > 1]
    checks.append(("one question per turn", not multi_q, multi_q[0][:90] if multi_q else ""))
    norm = [re.sub(r"\W+", " ", line.lower()).strip() for line in agent_lines if len(line) > 25]
    checks.append(("no repeated sentence", len(norm) == len(set(norm)), f"{len(norm) - len(set(norm))} repeats"))
    ended = bool(text) and "goodbye" in text[-1]
    checks.append(("ends with Goodbye" if not (TRANSFER in names) else "ends (transfer or goodbye)",
                   ended or TRANSFER in names, text[-1][-60:] if text else "no agent lines"))
    return checks
