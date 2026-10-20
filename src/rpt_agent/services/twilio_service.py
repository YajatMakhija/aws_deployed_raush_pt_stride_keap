from ..observability import WorkflowTrace
from .provider_http import ProviderError, ProviderService


class TwilioService(ProviderService):
    def send_sms(self, trace: WorkflowTrace, to: str, body: str) -> str:
        self._assert_outbound_allowed("twilio")
        base = self.settings.provider_url("twilio")
        account_sid, auth_token, from_number = self.settings.twilio_account_for(to)
        data = {"To": to, "From": from_number, "Body": body}
        if self.settings.mode("twilio") == "mock":
            url = base + "/messages"
        else:
            url = f"{base}/2010-04-01/Accounts/{account_sid}/Messages.json"
            if self.settings.public_base_url:
                data["StatusCallback"] = (
                    f"{self.settings.public_base_url.rstrip('/')}/api/v1/twilio/message-status"
                )
        response = self._request(
            trace,
            "twilio",
            "POST",
            url,
            operation="send_sms",
            data=data,
            auth=None
            if self.settings.mode("twilio") == "mock"
            else (account_sid, auth_token),
        )
        if response.status_code not in (200, 201):
            raise self._response_error("twilio", response, idempotent=False)
        sid = self._json_object(
            "twilio", response, operation="Twilio message", ambiguous=True
        ).get("sid")
        if not sid:
            raise ProviderError(
                "twilio", "missing_id", "Twilio response did not include a message sid", ambiguous=True
            )
        return str(sid)
