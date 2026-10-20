"""US test numbers (demo, 2026-10-20): +1 leads are called and texted from the US numbers, others are not."""

import base64
import hashlib
import hmac

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from rpt_agent import security
from rpt_agent.config import Settings, get_settings
from rpt_agent.observability import WorkflowTrace
from rpt_agent.providers import ProviderClients

US = {"us_vapi_phone_number_id": "vapi-us", "us_twilio_account_sid": "AC-us", "us_twilio_auth_token": "tok-us",
      "us_twilio_from_number": "+18885550100"}
DEFAULT = {"vapi_phone_number_id": "vapi-in", "twilio_account_sid": "AC-in", "twilio_auth_token": "tok-in",
           "twilio_from_number": "+15615550100"}


def test_a_us_lead_uses_the_us_numbers_and_others_the_default():
    s = Settings(**DEFAULT, **US)
    assert s.vapi_phone_number_for("+13105550100", "vapi-in") == "vapi-us"
    assert s.vapi_phone_number_for("+919800000000", "vapi-in") == "vapi-in"
    assert s.twilio_account_for("+13105550100") == ("AC-us", "tok-us", "+18885550100")
    assert s.twilio_account_for("+919800000000") == ("AC-in", "tok-in", "+15615550100")


def test_without_us_settings_everyone_uses_the_default():
    s = Settings(**DEFAULT)
    assert s.vapi_phone_number_for("+13105550100", "vapi-in") == "vapi-in"
    assert s.twilio_account_for("+13105550100") == ("AC-in", "tok-in", "+15615550100")


def test_a_half_set_us_twilio_account_is_not_used():
    # Texting from the US number without its own token would fail; better the default than a broken send.
    s = Settings(**DEFAULT, us_twilio_account_sid="AC-us", us_twilio_from_number="+18885550100")
    assert s.twilio_account_for("+13105550100") == ("AC-in", "tok-in", "+15615550100")


@pytest.mark.parametrize(("to", "account", "sender"), [
    ("+13105550100", "AC-us", "%2B18885550100"),
    ("+919800000000", "AC-in", "%2B15615550100"),
])
def test_the_sms_goes_out_from_the_right_account(to, account, sender):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["form"] = dict(item.split("=", 1) for item in request.content.decode().split("&"))
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(201, json={"sid": "SM-test"})

    settings = Settings(provider_mode="mock", twilio_mode="real", **DEFAULT, **US)
    clients = ProviderClients(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    assert clients.send_sms(WorkflowTrace("provider", "test", "t"), to, "synthetic") == "SM-test"
    assert seen["url"].endswith(f"/Accounts/{account}/Messages.json")
    assert seen["form"]["From"] == sender
    token = "tok-us" if account == "AC-us" else "tok-in"
    assert seen["auth"] == "Basic " + base64.b64encode(f"{account}:{token}".encode()).decode()


def _signed_request(token: str, url: str, form: dict[str, str]) -> Request:
    data = url + "".join(f"{k}{form[k]}" for k in sorted(form))
    signature = base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()
    return Request({"type": "http", "method": "POST", "path": "/api/v1/twilio/message-status", "query_string": b"",
                    "headers": [(b"x-twilio-signature", signature.encode())], "scheme": "https",
                    "server": ("demo.example.test", 443)})


@pytest.mark.anyio
@pytest.mark.parametrize(("token", "ok"), [("tok-in", True), ("tok-us", True), ("tok-other", False)])
async def test_twilio_webhooks_from_either_account_are_accepted(monkeypatch, token, ok):
    for key, value in {**DEFAULT, **US}.items():
        monkeypatch.setenv(key.upper(), value)
    monkeypatch.setenv("TWILIO_MODE", "real")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://demo.example.test")
    get_settings.cache_clear()
    form = {"MessageSid": "SM1", "MessageStatus": "delivered"}
    request = _signed_request(token, "https://demo.example.test/api/v1/twilio/message-status", form)
    try:
        if ok:
            await security.require_twilio_auth(request, form)
        else:
            with pytest.raises(HTTPException):
                await security.require_twilio_auth(request, form)
    finally:
        get_settings.cache_clear()


def test_keap_handoff_mode_is_read_from_the_new_name_and_the_old_one(monkeypatch):
    monkeypatch.delenv("KEAP_MODE", raising=False)
    monkeypatch.setenv("KEAP_HANDOFF_MODE", "mock")
    assert Settings(provider_mode="real").mode("keap") == "mock"
    monkeypatch.delenv("KEAP_HANDOFF_MODE")
    monkeypatch.setenv("KEAP_MODE", "mock")
    assert Settings(provider_mode="real").mode("keap") == "mock"
