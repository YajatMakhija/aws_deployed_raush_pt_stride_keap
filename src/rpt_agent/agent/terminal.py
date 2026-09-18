from __future__ import annotations

import json
import logging
import sys
from uuid import UUID, uuid4

import httpx

from ..config import get_settings

MAX_MESSAGE_CHARS = 4_000
MAX_MESSAGES = 12
MAX_CONVERSATION_CHARS = 20_000

HELP = """Commands:
  /add <lead-uuid>       Add a lead (maximum three)
  /remove <lead-uuid>    Remove a lead and clear chat history
  /leads                 Show loaded lead UUIDs
  /clear                 Clear messages and loaded leads
  /help                  Show this help
  /quit                  Exit
"""


def handle_command(
    line: str,
    lead_ids: list[str],
    messages: list[dict[str, str]],
) -> tuple[bool, bool, str]:
    command, _, argument = line.partition(" ")
    command = command.lower()
    argument = argument.strip()
    if command == "/quit":
        return True, False, "Goodbye."
    if command == "/help":
        return True, True, HELP.rstrip()
    if command == "/leads":
        return True, True, "\n".join(lead_ids) if lead_ids else "No leads loaded."
    if command == "/clear":
        lead_ids.clear()
        messages.clear()
        return True, True, "Chat and loaded leads cleared."
    if command in {"/add", "/remove"}:
        try:
            lead_id = str(UUID(argument))
        except (TypeError, ValueError):
            return True, True, f"Usage: {command} <lead-uuid>"
        if command == "/add":
            if lead_id in lead_ids:
                return True, True, "That lead is already loaded."
            if len(lead_ids) >= 3:
                return True, True, "Three leads are already loaded; remove one first."
            lead_ids.append(lead_id)
            return True, True, f"Loaded {lead_id}."
        if lead_id not in lead_ids:
            return True, True, "That lead is not loaded."
        lead_ids.remove(lead_id)
        messages.clear()
        return True, True, f"Removed {lead_id}; chat history cleared."
    if command.startswith("/"):
        return True, True, "Unknown command. Use /help."
    return False, True, ""


def _safe_error(status_code: int) -> str:
    return {
        401: "Authentication failed. Check DASHBOARD_API_TOKEN.",
        403: "Patient context is blocked until KIMI_PHI_APPROVED is enabled.",
        429: "The assistant request limit was reached; wait one minute and retry.",
        404: "One or more loaded leads no longer exist.",
        422: "The request was rejected. Check lead IDs and message limits.",
        502: "The assistant is temporarily unavailable.",
        503: "The assistant is disabled or incompletely configured.",
    }.get(status_code, f"Assistant request failed with HTTP {status_code}.")


def append_question(line: str, messages: list[dict[str, str]]) -> str | None:
    if len(line) > MAX_MESSAGE_CHARS:
        return f"Questions must be at most {MAX_MESSAGE_CHARS} characters."
    while messages and (
        len(messages) >= MAX_MESSAGES - 1
        or sum(len(message["content"]) for message in messages) + len(line)
        > MAX_CONVERSATION_CHARS
    ):
        del messages[:2]
    messages.append({"role": "user", "content": line})
    return None


def print_stream(response: httpx.Response) -> str:
    event = ""
    answer: list[str] = []
    completed = False
    started = False
    try:
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
                if event == "delta":
                    chunk = data["content"]
                    if not isinstance(chunk, str):
                        raise ValueError("invalid stream chunk")
                    if not started:
                        print("assistant> ", end="", flush=True)
                        started = True
                    print(chunk, end="", flush=True)
                    answer.append(chunk)
                elif event == "done":
                    completed = True
                elif event == "error":
                    raise ValueError("assistant stream failed")
            elif not line:
                event = ""
    finally:
        if started:
            print()
    result = "".join(answer)
    if not completed or not result:
        raise ValueError("assistant stream was incomplete")
    return result


def run_terminal(
    *,
    api_url: str | None = None,
    initial_leads: list[str] | None = None,
    user_id: str = "terminal-agent",
    email: str = "terminal-agent@local.test",
) -> None:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()
    if not settings.dashboard_api_token:
        raise RuntimeError("DASHBOARD_API_TOKEN is required for terminal agent access")
    lead_ids: list[str] = []
    messages: list[dict[str, str]] = []
    for value in initial_leads or []:
        handled, _, message = handle_command(f"/add {value}", lead_ids, messages)
        if handled and not message.startswith("Loaded"):
            raise ValueError(message)
    endpoint = f"{(api_url or settings.api_base_url).rstrip('/')}/api/v1/dashboard/assistant/stream"
    headers = {
        "X-Dashboard-Token": settings.dashboard_api_token,
        "X-Dashboard-User-ID": user_id,
        "X-Dashboard-User-Email": email,
    }
    print("RPT lead-scoped agent. Use /help for commands.")
    timeout = min(65.0, max(20.0, settings.request_timeout_seconds + 5))
    with httpx.Client(timeout=timeout) as client:
        while True:
            try:
                line = input("agent> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye.")
                return
            if not line:
                continue
            handled, keep_running, output = handle_command(line, lead_ids, messages)
            if handled:
                print(output)
                if not keep_running:
                    return
                continue
            error = append_question(line, messages)
            if error:
                print(error)
                continue
            try:
                with client.stream(
                    "POST",
                    endpoint,
                    headers={
                        **headers,
                        "Accept": "text/event-stream",
                        "X-Trace-ID": uuid4().hex,
                    },
                    json={"messages": messages, "lead_ids": lead_ids, "current_path": "terminal"},
                ) as response:
                    if not response.is_success:
                        messages.pop()
                        print(_safe_error(response.status_code))
                        continue
                    answer = print_stream(response)
                messages.append({"role": "assistant", "content": answer})
            except KeyboardInterrupt:
                messages.pop()
                print("Response cancelled.")
            except (httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                messages.pop()
                print("The backend could not be reached or returned an invalid response.")
