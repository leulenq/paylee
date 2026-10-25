from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SESSION_SECRET = "dev-only-session-secret-not-for-production"
DEV_ADMIN_PASSWORD = "admin"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str = "postgresql://swipebills:swipebills@127.0.0.1:5433/swipebills"
    schema_auto_apply: bool = True
    public_base_url: str = "http://127.0.0.1:8000"

    session_secret: str = DEV_SESSION_SECRET
    session_days: int = 30
    admin_password: str = DEV_ADMIN_PASSWORD
    allow_dev_login: bool = True
    apple_bundle_id: str = "enquanhone.SwipeBills"

    upload_dir: str = "uploads"
    max_upload_bytes: int = 10 * 1024 * 1024

    payment_provider: str = "fake"
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    # Live collection stays disabled until the actual program has been approved.
    live_payments_approved: bool = False
    funds_program_reference: str = ""
    data_encryption_key: str = ""
    account_fingerprint_key: str = ""
    storage_provider: str = "local"
    s3_bucket: str = ""
    s3_kms_key_id: str = ""
    aws_region: str = "us-east-1"
    malware_scanner: str = "clamav"  # "development" is explicitly non-production.
    clamscan_path: str = "clamscan"
    clamd_host: str = ""
    clamd_port: int = 3310
    ocr_provider: str = "manual"  # "textract" extracts; neither provider verifies debt.
    verification_hours: int = 48
    release_balance_minutes: int = 15
    staff_session_hours: int = 8
    worker_poll_seconds: float = 2.0

    # "console" logs emails; "smtp" sends them (any provider with SMTP: Postmark, SES, Resend, ...).
    email_provider: str = "console"
    email_from: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    email_worker_enabled: bool = True

    # "log" prints giver pushes; "apns" sends them. APNS_PRIVATE_KEY is the .p8 file's contents.
    push_provider: str = "log"
    apns_key_id: str = ""
    apns_team_id: str = ""
    apns_private_key: str = ""

    # Supply control: only this many bills are live at once; approved bills wait in a queue.
    max_live_bills: int = 50
    # Small minimums blunt stolen-card testing, which relies on cheap charges.
    min_contribution_cents: int = 500
    max_contributions_per_hour: int = 20
    # How long a giver's amount is held while they pay. Stripe Checkout sessions must stay open 30+ minutes.
    reservation_minutes: int = 35
    pass_cooldown_days: int = 3
    expiry_grace_days: int = 7

    def check_safe_for_environment(self) -> None:
        if self.environment != "production":
            return
        problems = []
        if self.session_secret == DEV_SESSION_SECRET or len(self.session_secret) < 32:
            problems.append("SESSION_SECRET must be set to 32+ random characters")
        if self.allow_dev_login:
            problems.append("ALLOW_DEV_LOGIN must be false")
        if self.schema_auto_apply:
            problems.append("SCHEMA_AUTO_APPLY must be false; migrate with a separate database owner")
        if self.payment_provider != "stripe":
            problems.append("PAYMENT_PROVIDER must be stripe")
        if self.email_provider != "smtp":
            problems.append("EMAIL_PROVIDER must be smtp")
        if not self.public_base_url.startswith("https://"):
            problems.append("PUBLIC_BASE_URL must be https")
        if not self.live_payments_approved or not self.funds_program_reference.strip():
            problems.append("Live payments require LIVE_PAYMENTS_APPROVED and FUNDS_PROGRAM_REFERENCE")
        if not self.stripe_secret_key or not self.stripe_webhook_secret:
            problems.append("Stripe credentials are required")
        if not self.smtp_host or not self.email_from:
            problems.append("SMTP_HOST and EMAIL_FROM are required")
        if not self.data_encryption_key:
            problems.append("DATA_ENCRYPTION_KEY is required")
        else:
            from cryptography.fernet import Fernet
            try:
                Fernet(self.data_encryption_key.encode())
            except (ValueError, TypeError):
                problems.append("DATA_ENCRYPTION_KEY must be a Fernet key")
        if len(self.account_fingerprint_key) < 32:
            problems.append("ACCOUNT_FINGERPRINT_KEY must be 32+ characters")
        if self.storage_provider != "s3" or not self.s3_bucket or not self.s3_kms_key_id:
            problems.append("Production documents require private S3 storage and an S3_KMS_KEY_ID")
        if self.malware_scanner != "clamav":
            problems.append("Production malware scanner must be clamav")
        if self.verification_hours <= 0 or self.release_balance_minutes <= 0:
            problems.append("Verification windows must be positive")
        if problems:
            raise RuntimeError("Unsafe production config: " + "; ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()
