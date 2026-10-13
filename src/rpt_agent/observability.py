from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import get_settings

trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")

SENSITIVE_KEYS = {
    "authorization", "token", "secret", "password", "date_of_birth", "dob", "phone",
    "phone_e164", "email", "body", "message", "transcript", "payload", "recording_url",
}


def _redact_string(value: str) -> str:
    value = re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", value, flags=re.IGNORECASE)
    def redact_phone(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        return "[REDACTED_PHONE]" if 10 <= len(digits) <= 15 else match.group(0)

    value = re.sub(r"(?<![\w])\+?[\d\s().-]{10,25}(?![\w-])", redact_phone, value)
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]", value)
    return value


def redact(value: Any, key: str = "") -> Any:
    if key.lower() in SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "service": getattr(record, "service", record.name),
            "trace_id": getattr(record, "trace_id", trace_id_var.get()),
            "event": getattr(record, "event", record.getMessage()),
        }
        details = getattr(record, "details", None)
        if details:
            event.update(redact(details))
        if record.exc_info:
            event["error_category"] = record.exc_info[0].__name__
            # Keep diagnostics useful without serializing tracebacks, locals, or raw payloads.
            event["error_message"] = redact(str(record.exc_info[1]), "exception")
        return json.dumps(event, separators=(",", ":"), default=str)


_configured = False


def quiet_http_libraries() -> None:
    """httpx logs every request url at INFO. Some urls carry an email or phone in the query
    string (Keap contact search), URL-encoded so the redaction regexes cannot see it."""
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def configure_logging(service: str) -> None:
    global _configured
    if _configured:
        return
    settings = get_settings()
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())
    formatter = JsonFormatter()
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    safe_service = re.sub(r"[^a-zA-Z0-9_-]+", "-", service).strip("-") or "service"
    # Each process owns its log file. RotatingFileHandler is not safe when several
    # API/worker/provider processes rotate the same file concurrently.
    file_handler = logging.handlers.RotatingFileHandler(
        Path(settings.log_dir) / f"rpt-agent-{safe_service}.jsonl",
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.handlers[:] = [console, file_handler]
    quiet_http_libraries()
    logging.getLogger("rpt_agent").info(
        "logging_configured", extra={"service": service, "event": "logging_configured"}
    )
    _configured = True


@dataclass
class WorkflowTrace:
    workflow: str
    service: str
    trace_id: str = ""
    step_number: int = 0
    started: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.trace_id = self.trace_id or trace_id_var.get() or uuid4().hex
        trace_id_var.set(self.trace_id)
        self.log("workflow_started")

    def log(self, event: str, level: int = logging.INFO, **details: Any) -> None:
        self.step_number += 1
        details.update({"workflow": self.workflow, "step": self.step_number})
        logging.getLogger(f"rpt_agent.{self.service}").log(
            level,
            event,
            extra={
                "service": self.service,
                "trace_id": self.trace_id,
                "event": event,
                "details": details,
            },
        )

    def complete(self, outcome: str = "success", **details: Any) -> None:
        details.update({"outcome": outcome, "duration_ms": round((time.monotonic() - self.started) * 1000)})
        self.log("workflow_completed", **details)

    def fail(self, exc: Exception, **details: Any) -> None:
        details.update({"outcome": "failed", "error_category": type(exc).__name__})
        self.log("workflow_failed", logging.ERROR, **details)
