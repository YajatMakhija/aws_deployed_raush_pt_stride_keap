from functools import cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    log_dir: Path = Path("logs")
    supabase_db_url: str = ""
    provider_mode: str = "mock"
    vapi_mode: str | None = None
    twilio_mode: str | None = None
    stride_mode: str | None = None
    keap_mode: str | None = None
    mock_base_url: str = "http://localhost:9000"
    api_base_url: str = "http://localhost:8000"
    public_base_url: str = ""
    dashboard_api_token: str = ""
    assistant_enabled: bool = False
    assistant_requests_per_minute: int = Field(default=10, ge=1, le=60)
    langsmith_tracing: bool = False
    langchain_tracing_v2: bool = False
    moonshot_api_key: str = ""
    kimi_model: str = "kimi-k3"
    kimi_phi_approved: bool = False
    kimi_phi_approval_reference: str = ""
    dashboard_public_url: str = ""
    n8n_intake_key_id: str = ""
    n8n_intake_secret: str = ""
    n8n_intake_auth_disabled: bool = False
    n8n_practice_slug: str = "rausch-pt"
    n8n_sheet_webhook_url: str = ""
    n8n_sheet_key_id: str = ""
    n8n_sheet_webhook_secret: str = ""
    sheet_sync_enabled: bool = False
    sheet_sync_poll_seconds: int = Field(default=30, ge=1, le=3600)
    sheet_sync_batch_size: int = Field(default=20, ge=1, le=100)
    sheet_sync_http_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    vapi_base_url: str = "https://api.vapi.ai"
    vapi_api_key: str = ""
    vapi_assistant_id: str = ""
    vapi_phone_number_id: str = ""
    vapi_webhook_secret: str = "local-vapi-secret"
    vapi_hmac_secret: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = "+15550000001"
    twilio_base_url: str = "https://api.twilio.com"
    stride_base_url: str = "https://demo.stridethera.com"
    stride_api_token: str = ""
    slot_token_secret: str = Field(default="local-slot-secret", min_length=8)
    keap_handoff_url: str = "http://localhost:9000/mock/keap/events"
    keap_handoff_secret: str = "local-keap-secret"
    worker_poll_seconds: int = 30
    # Master stop for anything that reaches a patient. Defaults on so a missing
    # variable can never silence outreach by accident; set false to suspend all
    # calls and SMS while leaving the dashboard readable.
    outbound_enabled: bool = True
    test_mode: bool = False
    test_cadence_day_minutes: int = Field(default=5, ge=1, le=1440)
    mock_scenario: str = "success"
    request_timeout_seconds: float = 10.0
    db_pool_timeout_seconds: float = 5.0
    http_retry_attempts: int = Field(default=3, ge=1, le=5)
    http_retry_base_seconds: float = Field(default=0.5, ge=0.1, le=5.0)
    retry_max_attempts: int = Field(default=5, ge=1, le=20)
    retry_base_seconds: int = Field(default=60, ge=1, le=3600)
    retry_max_seconds: int = Field(default=3600, ge=1, le=86400)

    def mode(self, provider: str) -> str:
        override = getattr(self, f"{provider}_mode", None)
        return override or self.provider_mode

    def provider_url(self, provider: str) -> str:
        if self.mode(provider) == "mock":
            return f"{self.mock_base_url.rstrip('/')}/mock/{provider}"
        return {
            "vapi": self.vapi_base_url,
            "twilio": self.twilio_base_url,
            "stride": self.stride_base_url,
        }[provider].rstrip("/")

    def runtime_errors(self, service: str) -> list[str]:
        errors: list[str] = []
        deployment_env = self.app_env.lower() in {"preproduction", "preprod", "staging", "production", "prod"}
        if service in {"api", "worker", "sheet-worker", "cli"} and not self.supabase_db_url:
            errors.append("SUPABASE_DB_URL is required")
        elif (
            service in {"api", "worker", "sheet-worker", "cli"}
            and "db.example.supabase.co" in self.supabase_db_url
        ):
            errors.append("SUPABASE_DB_URL still contains the example hostname")
        if self.provider_mode not in {"mock", "real"}:
            errors.append("PROVIDER_MODE must be mock or real")
        for provider in ("vapi", "twilio", "stride", "keap"):
            if self.mode(provider) not in {"mock", "real"}:
                errors.append(f"{provider.upper()}_MODE must be mock or real")
        if service in {"api", "worker"}:
            required: dict[str, str] = {}
            if self.mode("vapi") == "real":
                required.update(
                    VAPI_API_KEY=self.vapi_api_key,
                    VAPI_ASSISTANT_ID=self.vapi_assistant_id,
                    VAPI_PHONE_NUMBER_ID=self.vapi_phone_number_id,
                    VAPI_WEBHOOK_SECRET=self.vapi_webhook_secret,
                    PUBLIC_BASE_URL=self.public_base_url,
                )
            if self.mode("twilio") == "real":
                required.update(
                    TWILIO_ACCOUNT_SID=self.twilio_account_sid,
                    TWILIO_AUTH_TOKEN=self.twilio_auth_token,
                    TWILIO_FROM_NUMBER=self.twilio_from_number,
                    PUBLIC_BASE_URL=self.public_base_url,
                )
            if self.mode("stride") == "real":
                required.update(STRIDE_API_TOKEN=self.stride_api_token)
            if self.mode("keap") == "real":
                required.update(
                    KEAP_HANDOFF_URL=self.keap_handoff_url,
                    KEAP_HANDOFF_SECRET=self.keap_handoff_secret,
                )
            errors.extend(
                f"{name} is required when its provider is real"
                for name, value in required.items() if not value
            )
        if service == "api" and self.assistant_enabled:
            if self.langsmith_tracing or self.langchain_tracing_v2:
                errors.append("LangChain/LangSmith tracing must be disabled for the assistant")
            if not self.moonshot_api_key:
                errors.append("MOONSHOT_API_KEY is required when the assistant is enabled")
            if not self.kimi_model.strip():
                errors.append("KIMI_MODEL is required when the assistant is enabled")
            if deployment_env and not self.kimi_phi_approved:
                errors.append("KIMI_PHI_APPROVED must be true for the production assistant")
            if (deployment_env or self.kimi_phi_approved) and not (
                self.kimi_phi_approval_reference.strip()
            ):
                errors.append(
                    "KIMI_PHI_APPROVAL_REFERENCE is required for real-patient assistant use"
                )
        if deployment_env:
            if self.test_mode:
                errors.append("TEST_MODE must be false outside local development")
            if self.n8n_intake_auth_disabled:
                errors.append("N8N_INTAKE_AUTH_DISABLED must be false outside local development")
            database_url = self.supabase_db_url.lower()
            if not any(
                value in database_url for value in ("sslmode=require", "sslmode=verify-full")
            ):
                errors.append("SUPABASE_DB_URL must set sslmode=require or verify-full")
            if self.public_base_url and not self.public_base_url.startswith("https://"):
                errors.append("PUBLIC_BASE_URL must use HTTPS")
            if service == "api" and len(self.dashboard_api_token) < 32:
                errors.append("DASHBOARD_API_TOKEN must contain at least 32 characters")
            if self.log_level.upper() == "DEBUG":
                errors.append("LOG_LEVEL must not be DEBUG in a deployed environment")
            if service == "api":
                if not self.n8n_intake_key_id:
                    errors.append("N8N_INTAKE_KEY_ID is required")
                if len(self.n8n_intake_secret) < 32:
                    errors.append("N8N_INTAKE_SECRET must contain at least 32 characters")
            if "your-ngrok-domain" in self.public_base_url:
                errors.append("PUBLIC_BASE_URL still contains the example hostname")
            if self.mode("vapi") == "real" and self.vapi_webhook_secret == "local-vapi-secret":
                errors.append("VAPI_WEBHOOK_SECRET must be replaced outside local development")
            if self.mode("twilio") == "real" and self.twilio_account_sid == "AC" + "0" * 32:
                errors.append("TWILIO_ACCOUNT_SID still contains the example value")
            if self.mode("twilio") == "real" and self.twilio_from_number == "+15550000001":
                errors.append("TWILIO_FROM_NUMBER still contains the example value")
            if self.slot_token_secret in {
                "local-slot-secret",
                "replace-this-for-non-local-use",
            }:
                errors.append("SLOT_TOKEN_SECRET must be replaced outside local development")
            if self.mode("keap") == "real":
                if not self.keap_handoff_url.startswith("https://"):
                    errors.append("KEAP_HANDOFF_URL must use HTTPS")
                if "example.com" in self.keap_handoff_url:
                    errors.append("KEAP_HANDOFF_URL still contains the example hostname")
                if self.keap_handoff_secret == "local-keap-secret":
                    errors.append("KEAP_HANDOFF_SECRET must be replaced outside local development")
            if service == "sheet-worker" and self.sheet_sync_enabled:
                if not self.n8n_sheet_webhook_url.startswith("https://"):
                    errors.append("N8N_SHEET_WEBHOOK_URL must use HTTPS")
                elif "example.com" in self.n8n_sheet_webhook_url:
                    errors.append("N8N_SHEET_WEBHOOK_URL still contains the example hostname")
                if not self.n8n_sheet_key_id:
                    errors.append("N8N_SHEET_KEY_ID is required when Sheet sync is enabled")
                if len(self.n8n_sheet_webhook_secret) < 32:
                    errors.append("N8N_SHEET_WEBHOOK_SECRET must contain at least 32 characters")
        if service in {"api", "sheet-worker"} and self.sheet_sync_enabled:
            if not self.dashboard_public_url.startswith("https://"):
                errors.append("DASHBOARD_PUBLIC_URL must use HTTPS")
            elif "example.com" in self.dashboard_public_url:
                errors.append("DASHBOARD_PUBLIC_URL still contains the example hostname")
        if service == "sheet-worker" and self.sheet_sync_enabled:
            if not self.n8n_sheet_webhook_url:
                errors.append("N8N_SHEET_WEBHOOK_URL is required when Sheet sync is enabled")
            if not self.n8n_sheet_webhook_secret:
                errors.append("N8N_SHEET_WEBHOOK_SECRET is required when Sheet sync is enabled")
            if not self.n8n_practice_slug.strip():
                errors.append("N8N_PRACTICE_SLUG is required when Sheet sync is enabled")
        if self.retry_max_seconds < self.retry_base_seconds:
            errors.append("RETRY_MAX_SECONDS must be greater than or equal to RETRY_BASE_SECONDS")
        return errors


@cache
def get_settings() -> Settings:
    return Settings()
