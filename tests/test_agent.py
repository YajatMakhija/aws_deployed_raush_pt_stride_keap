from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, AIMessageChunk

import rpt_agent.agent.router as agent_routes
import rpt_agent.agent.service as agent_service
import rpt_agent.agent.terminal as agent_terminal
from rpt_agent.agent.terminal import append_question, handle_command, print_stream
from rpt_agent.api import app
from rpt_agent.config import get_settings


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class AgentConnection:
    def __init__(self, lead_id, *, is_test=True):
        self.lead_id = lead_id
        self.is_test = is_test
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        now = datetime.now(UTC)
        if "from leads l" in sql:
            return Result([{
                "id": self.lead_id,
                "full_name": "Synthetic Lead",
                "status": "in_progress",
                "status_reason": "Call +15555550123 or synthetic@example.test",
                "cadence_state": "active",
                "needs_review": False,
                "review_reason": None,
                "source_system": "synthetic_test",
                "lead_type": "Physical Therapy",
                "location": "Dana Point",
                "owner": "Test Owner",
                "is_test": self.is_test,
                "phone_e164": "+15555550123",
                "phone_original": "(555) 555-0123",
                "email": "synthetic@example.test",
                "date_of_birth": date(1990, 1, 1),
            }])
        if "from outreach_events oe" in sql:
            return Result([{
                "lead_id": self.lead_id,
                "id": 1,
                "channel": "call",
                "day_offset": 0,
                "status": "planned",
                "scheduled_for": now,
                "executed_at": None,
                "outcome": None,
                "description": "Initial call",
                "cadence_version_name": "Standard v10",
                "rn": 1,
            }])
        if "from appointments a" in sql:
            return Result([])
        if "from lead_status_history h" in sql:
            return Result([])
        if "from sms_messages m" in sql:
            return Result([{
                "lead_id": self.lead_id,
                "direction": "outbound",
                "body": "Call +15555550123; DOB 1990-01-01; email synthetic@example.test",
                "occurred_at": now,
                "delivered_at": now,
                "delivery_status": "delivered",
                "failure_reason": None,
                "rn": 1,
            }])
        if "from call_logs c" in sql:
            return Result([{
                "lead_id": self.lead_id,
                "dialed_at": now,
                "ended_at": now,
                "duration_seconds": 10,
                "answer_state": "answered",
                "ended_reason": "assistant-ended-call",
                "transcript_text": "Patient: my number is +15555550123",
                "summary_text": "Email synthetic@example.test",
                "rn": 1,
            }])
        raise AssertionError(sql)


def _headers():
    return {
        "X-Dashboard-Token": "x" * 32,
        "X-Dashboard-User-ID": "staff-1",
        "X-Dashboard-User-Email": "staff@example.test",
    }


@pytest.fixture(autouse=True)
def enable_agent(monkeypatch):
    monkeypatch.setenv("ASSISTANT_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_context_queries_only_selected_leads_and_redacts_free_text(monkeypatch):
    lead_id = uuid4()
    connection = AgentConnection(lead_id)

    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(agent_service, "transaction", fake_transaction)
    context = agent_service.load_lead_context([str(lead_id)], phi_approved=False)
    assert context[0]["lead"]["id"] == str(lead_id)
    rendered = str(context)
    assert "+15555550123" not in rendered
    assert "synthetic@example.test" not in rendered
    assert "1990-01-01" not in rendered
    assert "recording" not in rendered.lower()
    assert all("any(%s::uuid[])" in sql for sql, _ in connection.calls)
    assert all(params == ([str(lead_id)],) for _, params in connection.calls)


def test_phi_gate_runs_before_patient_context_queries(monkeypatch):
    connection = AgentConnection(uuid4(), is_test=False)

    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(agent_service, "transaction", fake_transaction)
    with pytest.raises(agent_service.PhiApprovalRequired):
        agent_service.load_lead_context([str(connection.lead_id)], phi_approved=False)
    assert len(connection.calls) == 1
    assert "select l.id,l.is_test" in connection.calls[0][0]


def test_context_text_budget_does_not_grow_after_exhaustion():
    bounded, remaining, truncated = agent_service._bound_text(
        {"first": "a" * 100, "second": "b" * 100}, 20
    )
    assert sum(len(value) for value in bounded.values()) <= 20
    assert remaining == 0
    assert truncated


def test_stream_prefix_redacts_values_split_across_chunks():
    pending = ""
    output = []
    for chunk in (
        "Call +1 555 ",
        "555 0199 or email result@",
        "example.test. token=sec",
        "ret-value. Done.",
    ):
        safe, pending = agent_service._safe_stream_prefix(pending + chunk)
        output.append(safe)
    output.append(agent_service._safe_free_text(pending))
    answer = "".join(output)
    assert "+1 555 555 0199" not in answer
    assert "result@example.test" not in answer
    assert "secret-value" not in answer
    assert "[REDACTED_PHONE]" in answer
    assert "[REDACTED_EMAIL]" in answer


def test_ambiguity_guard_names_only_loaded_leads():
    contexts = [
        {"lead": {"full_name": "Jane Lead", "display_id": "RPT-AAAA"}},
        {"lead": {"full_name": "John Lead", "display_id": "RPT-BBBB"}},
    ]
    assert agent_service.ambiguity_clarification("What is the next action?", contexts) == (
        "Which loaded lead do you mean: Jane Lead or John Lead?"
    )
    assert agent_service.ambiguity_clarification("What is Jane Lead's next action?", contexts) is None
    assert agent_service.ambiguity_clarification("Compare these leads", contexts) is None
    assert agent_service.ambiguity_clarification("What does Review Queue do?", contexts) is None


def test_unselected_lead_identifier_is_refused():
    selected = uuid4()
    contexts = [{"lead": {"id": str(selected), "display_id": f"RPT-{str(selected)[:8]}"}}]
    assert agent_service.unselected_lead_refusal(f"Tell me about {uuid4()}", contexts) == (
        "That lead is not loaded in this conversation. Add it before asking about it."
    )


def test_agent_endpoint_requires_auth_and_caps_leads(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()

    async def fake_answer(messages, lead_ids, current_path):
        assert messages[-1]["content"] == "Hello"
        assert current_path == "terminal"
        return f"Loaded {len(lead_ids)} leads"

    audits = []
    monkeypatch.setattr(agent_routes, "answer_question", fake_answer)
    monkeypatch.setattr(agent_routes, "_audit_request", lambda *args: audits.append(args))
    client = TestClient(app)
    try:
        assert client.post(
            "/api/v1/dashboard/assistant",
            json={"messages": [{"role": "user", "content": "Hello"}]},
        ).status_code == 401
        ids = [str(uuid4()) for _ in range(4)]
        too_many = client.post(
            "/api/v1/dashboard/assistant",
            headers=_headers(),
            json={"messages": [{"role": "user", "content": "Hello"}], "lead_ids": ids},
        )
        assert too_many.status_code == 422
        duplicated = client.post(
            "/api/v1/dashboard/assistant",
            headers=_headers(),
            json={
                "messages": [{"role": "user", "content": "Hello"}],
                "lead_ids": [ids[0], ids[0]],
                "current_path": "terminal",
            },
        )
        assert duplicated.status_code == 200
        assert duplicated.json()["answer"] == "Loaded 1 leads"
        assert audits[0][1] == [ids[0]]
    finally:
        get_settings.cache_clear()


def test_agent_endpoint_validates_messages(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()
    client = TestClient(app)
    try:
        invalid_uuid = client.post(
            "/api/v1/dashboard/assistant",
            headers=_headers(),
            json={
                "messages": [{"role": "user", "content": "Hello"}],
                "lead_ids": ["not-a-uuid"],
            },
        )
        assert invalid_uuid.status_code == 422
        assistant_last = client.post(
            "/api/v1/dashboard/assistant",
            headers=_headers(),
            json={"messages": [{"role": "assistant", "content": "Hello"}]},
        )
        assert assistant_last.status_code == 422
    finally:
        get_settings.cache_clear()


def test_agent_stream_endpoint_emits_sse_chunks(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    get_settings.cache_clear()

    def fake_stream(messages, lead_ids, current_path):
        assert messages[-1]["content"] == "Hello"
        assert lead_ids == []
        assert current_path == "terminal"

        async def chunks():
            yield "First chunk. "
            yield "Second chunk."

        return chunks()

    monkeypatch.setattr(agent_routes, "stream_answer", fake_stream)
    monkeypatch.setattr(agent_routes, "_audit_request", lambda *_args: None)
    client = TestClient(app)
    with client.stream(
        "POST",
        "/api/v1/dashboard/assistant/stream",
        headers=_headers(),
        json={"messages": [{"role": "user", "content": "Hello"}]},
    ) as response:
        body = "".join(response.iter_text())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert body.count("event: delta") == 2
    assert "First chunk." in body
    assert "event: done" in body


def test_agent_endpoint_is_fail_closed_when_disabled(monkeypatch):
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    monkeypatch.setenv("ASSISTANT_ENABLED", "false")
    get_settings.cache_clear()
    client = TestClient(app)
    response = client.post(
        "/api/v1/dashboard/assistant",
        headers=_headers(),
        json={"messages": [{"role": "user", "content": "Hello"}]},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "the assistant is disabled"


def test_agent_audit_rate_limit_is_database_backed(monkeypatch):
    class AuditConnection:
        def __init__(self, recent=0):
            self.calls = []
            self.recent = recent

        def execute(self, sql, params=None):
            self.calls.append((" ".join(sql.split()), params))
            if "select count(*)" in sql:
                return Result([{"count": self.recent}])
            return Result([])

    connection = AuditConnection()

    @contextmanager
    def fake_transaction():
        yield connection

    monkeypatch.setattr(agent_routes, "transaction", fake_transaction)
    lead_id = str(uuid4())
    agent_routes._audit_request(
        agent_routes.DashboardActor("staff-1", "staff@example.test"),
        [lead_id],
        3,
        "trace-1",
    )
    assert any("pg_advisory_xact_lock" in sql for sql, _ in connection.calls)
    insert = next(call for call in connection.calls if "insert into dashboard_audit_log" in call[0])
    assert insert[1][2] == "trace-1"
    assert "selected_lead_ids" in str(insert[1][3])

    blocked = AuditConnection(recent=10)

    @contextmanager
    def blocked_transaction():
        yield blocked

    monkeypatch.setattr(agent_routes, "transaction", blocked_transaction)
    with pytest.raises(agent_routes.AgentRateLimitError):
        agent_routes._audit_request(
            agent_routes.DashboardActor("staff-1", "staff@example.test"),
            [lead_id],
            3,
            "trace-2",
        )
    assert not any("insert into dashboard_audit_log" in sql for sql, _ in blocked.calls)


@pytest.mark.asyncio
async def test_missing_kimi_key_fails_before_database(monkeypatch):
    settings = get_settings().model_copy(update={"moonshot_api_key": ""})
    monkeypatch.setattr(agent_service, "get_settings", lambda: settings)
    with pytest.raises(agent_service.AgentConfigurationError):
        await agent_service.answer_question(
            [{"role": "user", "content": "Hello"}], [], "terminal"
        )


@pytest.mark.asyncio
async def test_phi_approval_requires_a_reference_before_database(monkeypatch):
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    monkeypatch.setenv("KIMI_PHI_APPROVED", "true")
    monkeypatch.delenv("KIMI_PHI_APPROVAL_REFERENCE", raising=False)
    monkeypatch.setattr(
        agent_service,
        "load_lead_context",
        lambda *_args, **_kwargs: pytest.fail("invalid PHI approval must fail before database access"),
    )
    get_settings.cache_clear()
    try:
        with pytest.raises(agent_service.AgentConfigurationError):
            await agent_service.answer_question(
                [{"role": "user", "content": "Hello"}], [str(uuid4())], "terminal"
            )
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_no_leads_asks_the_model_without_patient_context(monkeypatch):
    """"What is a cadence?" must reach the model; the keyword gate used to refuse it."""
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    seen = {}

    def fake_create_agent(**kwargs):
        class Agent:
            async def ainvoke(self, payload):
                seen["messages"] = payload["messages"]
                from langchain_core.messages import AIMessage
                return {"messages": [AIMessage(content="A cadence is the 14-day outreach schedule.")]}
        return Agent()

    monkeypatch.setattr(agent_service, "create_agent", fake_create_agent)
    monkeypatch.setattr(
        agent_service, "load_lead_context", lambda *a, **k: pytest.fail("no patient data without a lead")
    )
    get_settings.cache_clear()
    try:
        answer = await agent_service.answer_question(
            [{"role": "user", "content": "What is a cadence?"}], [], "terminal"
        )
        assert answer.startswith("A cadence is")
        assert "selected-lead" not in " ".join(str(m) for m in seen["messages"]).lower()
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_dashboard_feature_question_does_not_load_patient_context(monkeypatch):
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    monkeypatch.setattr(
        agent_service,
        "load_lead_context",
        lambda *_args, **_kwargs: pytest.fail("feature help must not load patient context"),
    )

    async def fake_invoke(_settings, _messages, contexts, _current_path):
        assert contexts == []
        return "Open Review Queue from the dashboard."

    monkeypatch.setattr(agent_service, "_invoke_agent", fake_invoke)
    get_settings.cache_clear()
    try:
        answer = await agent_service.answer_question(
            [{"role": "user", "content": "What does Review Queue do?"}],
            [str(uuid4())],
            "terminal",
        )
        assert answer.startswith("Open Review Queue")
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_real_lead_requires_phi_approval(monkeypatch):
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    monkeypatch.setenv("KIMI_PHI_APPROVED", "false")
    get_settings.cache_clear()

    def fake_load(_, *, phi_approved):
        assert not phi_approved
        raise agent_service.PhiApprovalRequired()

    monkeypatch.setattr(
        agent_service,
        "load_lead_context",
        fake_load,
    )
    try:
        with pytest.raises(agent_service.PhiApprovalRequired):
            await agent_service.answer_question(
                [{"role": "user", "content": "Hello"}], [str(uuid4())], "terminal"
            )
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_kimi_payload_is_scoped_redacted_and_mocked(monkeypatch):
    lead_id = uuid4()
    connection = AgentConnection(lead_id)
    captured = {}

    @contextmanager
    def fake_transaction():
        yield connection

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured["config"] = kwargs

    class FakeAgent:
        async def ainvoke(self, payload):
            captured["messages"] = payload["messages"]
            return {"messages": [AIMessage(content="Email result@example.test or call +15555550199")]}

    def fake_create_agent(*, model, tools, system_prompt):
        captured["agent"] = (model, tools, system_prompt)
        return FakeAgent()

    monkeypatch.setattr(agent_service, "transaction", fake_transaction)
    monkeypatch.setattr(agent_service, "ChatOpenAI", FakeChatOpenAI)
    monkeypatch.setattr(agent_service, "create_agent", fake_create_agent)
    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    monkeypatch.setenv("REQUEST_TIMEOUT_SECONDS", "999")
    get_settings.cache_clear()
    try:
        answer = await agent_service.answer_question(
            [{
                "role": "user",
                "content": "DOB: 1990-01-01 token=secret-value. What is the next action?",
            }],
            [str(lead_id)],
            "terminal",
        )
    finally:
        get_settings.cache_clear()

    payload = "\n".join(str(message.content) for message in captured["messages"])
    assert all(not isinstance(message, AIMessage) for message in captured["messages"])
    for forbidden in (
        str(lead_id),
        "+15555550123",
        "synthetic@example.test",
        "1990-01-01",
        "secret-value",
        "recording_url",
        "provider_ref",
    ):
        assert forbidden not in payload
    assert captured["config"]["model"] == "kimi-k3"
    assert captured["config"]["reasoning_effort"] == "low"
    assert captured["config"]["max_retries"] == 1
    assert captured["config"]["timeout"] == 60
    assert "result@example.test" not in answer
    assert "+15555550199" not in answer


@pytest.mark.asyncio
async def test_kimi_streams_multiple_redacted_chunks(monkeypatch):
    class FakeChatOpenAI:
        def __init__(self, **_kwargs):
            pass

    class FakeAgent:
        async def astream(self, _payload, *, stream_mode):
            assert stream_mode == "messages"
            yield AIMessageChunk(content="First sentence. "), {}
            yield AIMessageChunk(content="Email result@"), {}
            yield AIMessageChunk(content="example.test. Final sentence."), {}

    monkeypatch.setattr(agent_service, "ChatOpenAI", FakeChatOpenAI)
    monkeypatch.setattr(agent_service, "create_agent", lambda **_kwargs: FakeAgent())
    settings = get_settings().model_copy(update={"moonshot_api_key": "test-key"})
    chunks = [
        chunk
        async for chunk in agent_service._stream_agent(
            settings,
            [{"role": "user", "content": "Hello"}],
            [],
            "terminal",
        )
    ]
    answer = "".join(chunks)
    assert len(chunks) >= 2
    assert "First sentence." in answer
    assert "result@example.test" not in answer
    assert "[REDACTED_EMAIL]" in answer


def test_terminal_prints_and_collects_stream(capsys):
    response = httpx.Response(
        200,
        content=(
            'event: delta\ndata: {"content": "First "}\n\n'
            'event: delta\ndata: {"content": "second."}\n\n'
            'event: done\ndata: {"trace_id": "safe-trace"}\n\n'
        ),
    )
    assert print_stream(response) == "First second."
    assert capsys.readouterr().out == "assistant> First second.\n"


def test_terminal_cancels_stream_without_traceback(monkeypatch, capsys):
    captured = {}

    class Response:
        is_success = True

    class Stream:
        def __enter__(self):
            return Response()

        def __exit__(self, *_args):
            return False

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, _method, _url, *, headers, json):
            captured["headers"] = headers
            captured["messages"] = list(json["messages"])
            return Stream()

    inputs = iter(("Hello", "/quit"))
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "x" * 32)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    monkeypatch.setattr(agent_terminal.httpx, "Client", Client)
    monkeypatch.setattr(
        agent_terminal,
        "print_stream",
        lambda _response: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    get_settings.cache_clear()
    try:
        agent_terminal.run_terminal(api_url="http://localhost:8000")
    finally:
        get_settings.cache_clear()
    output = capsys.readouterr().out
    assert "Response cancelled." in output
    assert "Goodbye." in output
    assert len(captured["headers"]["X-Trace-ID"]) == 32
    assert captured["messages"] == [{"role": "user", "content": "Hello"}]


def test_terminal_selection_commands_clear_cross_lead_history():
    lead_id = str(uuid4())
    other_id = str(uuid4())
    leads = []
    messages = [{"role": "user", "content": "old"}]
    assert handle_command(f"/add {lead_id}", leads, messages)[2].startswith("Loaded")
    assert handle_command(f"/add {other_id}", leads, messages)[2].startswith("Loaded")
    handled, keep_running, output = handle_command(f"/remove {lead_id}", leads, messages)
    assert handled and keep_running and "cleared" in output
    assert leads == [other_id]
    assert messages == []
    assert handle_command("/clear", leads, messages)[2] == "Chat and loaded leads cleared."
    assert handle_command("/quit", leads, messages)[:2] == (True, False)


def test_terminal_rejects_a_fourth_lead():
    leads = [str(uuid4()) for _ in range(3)]
    output = handle_command(f"/add {uuid4()}", leads, [])[2]
    assert output == "Three leads are already loaded; remove one first."
    assert len(leads) == 3


def test_terminal_bounds_chat_history_before_sending():
    messages = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": str(index)}
        for index in range(12)
    ]
    assert append_question("next", messages) is None
    assert len(messages) == 11
    assert messages[-1] == {"role": "user", "content": "next"}
    snapshot = list(messages)
    assert append_question("x" * 4001, messages) == "Questions must be at most 4000 characters."
    assert messages == snapshot
