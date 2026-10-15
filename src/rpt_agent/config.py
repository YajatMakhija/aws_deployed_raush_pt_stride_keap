from datetime import date
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
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
    # Any OpenAI-compatible chat API; the base URL and model are settings so a provider
    # change needs no code change.
    openai_api_key: str = ""
    assistant_base_url: str = "https://api.openai.com/v1"
    assistant_model: str = "gpt-5.6-luna"
    assistant_phi_approved: bool = False
    assistant_phi_approval_reference: str = ""
    dashboard_public_url: str = ""
    # The Google Sheet is the only way leads come in (client, 2026-10-15): the dashboard's "Add
    # lead" is switched off unless this is turned on (local tests, emergencies).
    dashboard_lead_create_enabled: bool = False
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
    # "n8n": the sheet-worker posts snapshots to n8n and n8n owns the Sheet (the old setup).
    # "direct": the sheet-worker reads the Sheet itself (commands, profile edits) and writes
    # results straight into it with a Google service account; the n8n Sheet workflows must be
    # switched off, or both would act on the same row.
    sheet_sync_mode: Literal["n8n", "direct"] = "n8n"
    google_sheet_credentials_file: str = "/run/secrets/google-sheet-sa.json"
    google_sheet_id: str = ""
    google_sheet_tab: str = "Sheet1"
    google_sheet_header_row: int = Field(default=2, ge=1, le=20)
    # A row left "Processing"/"Retrying" this long (worker stopped mid-command) is retried
    # with the same Action Request ID, which replays the stored result.
    sheet_stuck_seconds: int = Field(default=120, ge=30, le=3600)
    # Most polls read only five narrow columns; the profile-edit check needs every column and
    # runs on a full read this often.
    sheet_full_read_seconds: int = Field(default=300, ge=30, le=86400)
    # A command row runs only once its cells stayed unchanged this long (staff still typing).
    sheet_settle_seconds: int = Field(default=60, ge=0, le=3600)
    # Commands started per poll; the rest of a big pasted block waits for the next polls.
    sheet_max_commands_per_poll: int = Field(default=50, ge=1, le=1000)
    # Nightly archive: rows of finished leads (booked, do not contact, declined, closed, wrong
    # number, or cadence over) untouched for SHEET_ARCHIVE_DAYS move to the archive tab at
    # SHEET_ARCHIVE_HOUR (Pacific). Off until the clinic agrees.
    sheet_archive_enabled: bool = False
    sheet_archive_days: int = Field(default=30, ge=1, le=3650)
    sheet_archive_hour: int = Field(default=3, ge=0, le=23)
    google_sheet_archive_tab: str = "Archive"
    # Stride CSV exports arrive over SFTP into the inbox; the stride-worker moves
    # each file to archive (imported) or error (unusable) once it is done.
    stride_import_enabled: bool = False
    stride_practice_slug: str = "rausch-pt"
    stride_inbox_dir: Path = Path("stride/incoming")
    stride_archive_dir: Path = Path("stride/archive")
    stride_error_dir: Path = Path("stride/error")
    stride_import_poll_seconds: int = Field(default=60, ge=5, le=3600)
    # Stride writes files under their final name; one untouched for this long is complete.
    stride_file_settle_seconds: int = Field(default=60, ge=0, le=3600)
    stride_import_max_attempts: int = Field(default=3, ge=1, le=20)
    # Bigger files go straight to the error folder; the importer streams, so this
    # guards disk and import time, not memory.
    stride_max_file_mb: int = Field(default=2048, ge=1, le=102400)
    # Skip and report bad rows, but reject the whole file above this share.
    # A file is rejected only when bad rows are BOTH more than this percent AND at least
    # STRIDE_MIN_BAD_ROWS_TO_REJECT (like Snowflake's SKIP_FILE_<n>/<n>%): one typo in a small
    # delta file must not throw the whole file away. Bad rows are always skipped and listed.
    stride_max_bad_row_percent: float = Field(default=5.0, ge=0, le=100)
    stride_min_bad_rows_to_reject: int = Field(default=5, ge=1)
    # Voice-agent booking engine (find-slots / book-appointment tools).
    # Stride demo slots only exist in the past, so testing pins "today" here.
    # Never set in production.
    booking_today_override: date | None = None
    booking_horizon_days: int = Field(default=31, ge=1, le=31)
    booking_min_notice_minutes: int = Field(default=120, ge=0, le=10080)
    booking_sync_seconds: int = Field(default=300, ge=30, le=3600)
    # A search refreshes a location's cache itself when it is older than this.
    booking_cache_max_age_seconds: int = Field(default=900, ge=60, le=86400)
    booking_hold_seconds: int = Field(default=300, ge=30, le=1800)
    # Vapi waits about 30 s for a tool; one Stride request may take this long and
    # is never retried inside a live call.
    booking_stride_timeout_seconds: float = Field(default=6.0, ge=1.0, le=20.0)
    booking_tool_deadline_seconds: float = Field(default=24.0, ge=5.0, le=60.0)
    # Stride requires an address; the client does not collect one on the call.
    booking_default_address_1: str = "Address not provided"
    booking_default_city: str = "Laguna Niguel"
    booking_default_state: str = "CA"
    booking_default_zip: str = "92677"
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
    # Keap contact sync (phase 2A plans, phase 2B delivers). Off until a practice has
    # a connection, a mapping and keap_sync_start_at; per-practice choices live in
    # practice_settings / keap_appointment_types.
    keap_sync_enabled: bool = False
    keap_include_test_leads: bool = False  # true only while testing against the Keap sandbox
    keap_plan_batch: int = Field(default=500, ge=1, le=5000)
    keap_client_id: str = ""
    keap_client_secret: str = ""
    keap_redirect_uri: str = "https://stride.aibolt.ai/api/v1/keap/oauth/callback"
    keap_api_base_url: str = "https://api.infusionsoft.com/crm/rest/v2"
    keap_token_url: str = "https://api.infusionsoft.com/token"
    keap_authorize_url: str = "https://accounts.infusionsoft.com/app/oauth/authorize"
    keap_sync_poll_seconds: int = Field(default=30, ge=1, le=3600)
    keap_sync_batch_size: int = Field(default=20, ge=1, le=200)
    keap_sync_max_attempts: int = Field(default=3, ge=1, le=20)
    keap_max_requests_per_minute: int = Field(default=120, ge=1, le=1500)
    keap_max_requests_per_day: int = Field(default=40000, ge=1, le=150000)
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
    # Supabase's session pooler allows only 15 clients in total, shared by every
    # container and local script, so each process keeps a small pool.
    db_pool_max_size: int = Field(default=3, ge=1, le=20)
    db_pool_max_idle_seconds: float = 60.0
    http_retry_attempts: int = Field(default=3, ge=1, le=5)
    http_retry_base_seconds: float = Field(default=0.5, ge=0.1, le=5.0)
    retry_max_attempts: int = Field(default=5, ge=1, le=20)
    retry_base_seconds: int = Field(default=60, ge=1, le=3600)
    retry_max_seconds: int = Field(default=3600, ge=1, le=86400)

    @field_validator("booking_today_override", mode="before")
    @classmethod
    def _blank_override_is_unset(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

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
        if service in {"api", "worker", "sheet-worker", "stride-worker", "booking-worker", "keap-worker", "cli"} and not self.supabase_db_url:
            errors.append("SUPABASE_DB_URL is required")
        elif (
            service in {"api", "worker", "sheet-worker", "stride-worker", "booking-worker", "keap-worker", "cli"}
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
            if not self.openai_api_key:
                errors.append("OPENAI_API_KEY is required when the assistant is enabled")
            if not self.assistant_model.strip():
                errors.append("ASSISTANT_MODEL is required when the assistant is enabled")
            if deployment_env and not self.assistant_phi_approved:
                errors.append("ASSISTANT_PHI_APPROVED must be true for the production assistant")
            if (deployment_env or self.assistant_phi_approved) and not (
                self.assistant_phi_approval_reference.strip()
            ):
                errors.append(
                    "ASSISTANT_PHI_APPROVAL_REFERENCE is required for real-patient assistant use"
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
            if service == "sheet-worker" and self.sheet_sync_enabled and self.sheet_sync_mode == "n8n":
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
        if service == "sheet-worker" and self.sheet_sync_enabled and self.sheet_sync_mode == "direct":
            if not self.google_sheet_id:
                errors.append("GOOGLE_SHEET_ID is required when SHEET_SYNC_MODE=direct")
            if not Path(self.google_sheet_credentials_file).is_file():
                errors.append("GOOGLE_SHEET_CREDENTIALS_FILE does not point to a readable key file")
        if service == "sheet-worker" and self.sheet_sync_enabled:
            if self.sheet_sync_mode == "n8n" and not self.n8n_sheet_webhook_url:
                errors.append("N8N_SHEET_WEBHOOK_URL is required when Sheet sync is enabled")
            if self.sheet_sync_mode == "n8n" and not self.n8n_sheet_webhook_secret:
                errors.append("N8N_SHEET_WEBHOOK_SECRET is required when Sheet sync is enabled")
            if not self.n8n_practice_slug.strip():
                errors.append("N8N_PRACTICE_SLUG is required when Sheet sync is enabled")
        if service in {"api", "keap-worker"} and self.keap_sync_enabled:
            if not self.keap_client_id or not self.keap_client_secret:
                errors.append("KEAP_CLIENT_ID and KEAP_CLIENT_SECRET are required when Keap sync is enabled")
            if deployment_env and not self.keap_redirect_uri.startswith("https://"):
                errors.append("KEAP_REDIRECT_URI must use HTTPS")
        if self.retry_max_seconds < self.retry_base_seconds:
            errors.append("RETRY_MAX_SECONDS must be greater than or equal to RETRY_BASE_SECONDS")
        return errors


@cache
def get_settings() -> Settings:
    return Settings()
