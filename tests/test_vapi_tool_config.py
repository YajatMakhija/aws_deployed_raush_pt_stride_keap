import json
from pathlib import Path


def test_vapi_tools_are_sync_strict_and_have_concise_start_messages():
    tools = json.loads(Path("config/vapi_tools.json").read_text(encoding="utf-8"))
    assert {tool["function"]["name"] for tool in tools} == {
        "check_availability", "create_appointment", "update_lead_status"
    }
    expected_paths = {
        "check_availability": "/api/v1/tools/check-availability",
        "create_appointment": "/api/v1/tools/create-appointment",
        "update_lead_status": "/api/v1/webhooks/vapi/lead-status",
    }
    for tool in tools:
        assert tool["async"] is False
        assert tool["function"]["strict"] is True
        assert tool["function"]["parameters"]["additionalProperties"] is False
        static_keys = {item["key"] for item in tool["parameters"]}
        assert "lead_id" in static_keys
        assert "lead_id" not in tool["function"]["parameters"]["properties"]
        assert tool["server"]["url"].endswith(expected_paths[tool["function"]["name"]])
        assert tool["server"]["credentialId"]
    assert "outreach_event_id" in {item["key"] for item in tools[1]["parameters"]}
    assert "outreach_event_id" in {item["key"] for item in tools[2]["parameters"]}
    slow_tools = tools[:2]
    assert all(tool["messages"][0]["type"] == "request-start" for tool in slow_tools)


def test_callback_is_recorded_immediately_after_confirmation():
    prompt = Path("config/vapi_assistant_prompt.md").read_text(encoding="utf-8")
    assert "call `update_lead_status` immediately" in prompt
    assert "Do not speak again or end the call until the tool" in prompt
