import os
from dotenv import load_dotenv

load_dotenv()


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "sim"}


def _bounded_int(value: str | None, default: int, maximum: int) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, 1), maximum)


def _bounded_float(
    value: str | None, default: float, minimum: float, maximum: float
) -> float:
    try:
        parsed = float(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


class Config:
    AUDIT_LOG_LEVEL = os.getenv("IREO_AUDIT_LOG_LEVEL", "WARNING").upper()
    IREO_ONEDRIVE_PATIENTS_PATH = os.getenv(
        "IREO_ONEDRIVE_PATIENTS_PATH",
        r"D:\OneDrive\Pasta pacientes 2026",
    )
    IREO_RADIOLOGY_QUARANTINE_PATH = os.getenv(
        "IREO_RADIOLOGY_QUARANTINE_PATH",
        r"D:\IREO_Radiology_Quarantine",
    )
    IREO_ARCHIVE_TOOL_PATH = os.getenv(
        "IREO_ARCHIVE_TOOL_PATH",
        r"C:\Program Files\WinRAR\UnRAR.exe",
    )
    IREO_ARCHIVE_TIMEOUT_SECONDS = _bounded_int(
        os.getenv("IREO_ARCHIVE_TIMEOUT_SECONDS"),
        default=1800,
        maximum=86400,
    )
    IREO_TRANSFERNOW_CONNECT_TIMEOUT_SECONDS = _bounded_int(
        os.getenv("IREO_TRANSFERNOW_CONNECT_TIMEOUT_SECONDS"), 30, 300
    )
    IREO_TRANSFERNOW_READ_TIMEOUT_SECONDS = _bounded_int(
        os.getenv("IREO_TRANSFERNOW_READ_TIMEOUT_SECONDS"), 1800, 86400
    )
    IREO_TRANSFERNOW_MAX_DOWNLOAD_BYTES = _bounded_int(
        os.getenv("IREO_TRANSFERNOW_MAX_DOWNLOAD_BYTES"), 10737418240, 1099511627776
    )
    IREO_BROWSER_HEADLESS = False  # Piloto supervisionado: sempre visível.
    IREO_BROWSER_DEBUG = _bool(os.getenv("IREO_BROWSER_DEBUG"))
    IREO_BROWSER_DOWNLOAD_TIMEOUT_SECONDS = _bounded_int(
        os.getenv("IREO_BROWSER_DOWNLOAD_TIMEOUT_SECONDS"), 3600, 86400
    )
    IREO_AUTO_SELECT_UNAMBIGUOUS = _bool(
        os.getenv("IREO_AUTO_SELECT_UNAMBIGUOUS"), False
    )
    IREO_AUTO_SELECT_MIN_SCORE = _bounded_float(
        os.getenv("IREO_AUTO_SELECT_MIN_SCORE"), 0.95, 0.0, 1.0
    )
    IREO_INTAKE_DATABASE_PATH = os.getenv(
        "IREO_INTAKE_DATABASE_PATH", "data/ireo_intake.db"
    )
    IREO_AUTO_RUN_ENABLED = _bool(os.getenv("IREO_AUTO_RUN_ENABLED"), False)
    IREO_AUTO_RUN_MAX_MESSAGES = _bounded_int(
        os.getenv("IREO_AUTO_RUN_MAX_MESSAGES"), 3, 20
    )
    IREO_AUTO_RUN_ALLOW_COPY = _bool(os.getenv("IREO_AUTO_RUN_ALLOW_COPY"), False)
    IREO_AUTO_RUN_REQUIRE_EXACT_MATCH = _bool(
        os.getenv("IREO_AUTO_RUN_REQUIRE_EXACT_MATCH"), True
    )
    IREO_AUTO_RUN_NOTIFY_ON_SUCCESS = _bool(
        os.getenv("IREO_AUTO_RUN_NOTIFY_ON_SUCCESS"), False
    )
    IREO_AUTO_RUN_BROWSER_MODE = os.getenv(
        "IREO_AUTO_RUN_BROWSER_MODE", "review"
    ).strip().casefold()
    IREO_AUTO_RUN_SUMMARY_PATH = os.getenv(
        "IREO_AUTO_RUN_SUMMARY_PATH", "data/last_auto_run_summary.json"
    )
    IREO_AUTO_RUN_START_DATE = os.getenv("IREO_AUTO_RUN_START_DATE", "").strip()
    IREO_AUTO_RUN_REVIEW_REPORT_PATH = os.getenv(
        "IREO_AUTO_RUN_REVIEW_REPORT_PATH", "data/radiology_review_required.txt"
    )
    IREO_AUTO_RUN_DEBUG = _bool(os.getenv("IREO_AUTO_RUN_DEBUG"), False)
    CLINICORP_SUBSCRIBER_ID = os.getenv("CLINICORP_SUBSCRIBER_ID")
    CLINICORP_BUSINESS_ID = os.getenv("CLINICORP_BUSINESS_ID")
    CLINICORP_API_USER = os.getenv("CLINICORP_API_USER")
    CLINICORP_TOKEN = os.getenv("CLINICORP_TOKEN")
    CLINICORP_BASE_URL = os.getenv("CLINICORP_BASE_URL")
    GMAIL_CREDENTIALS_FILE = os.getenv(
        "GMAIL_CREDENTIALS_FILE",
        "gmail_credentials.json",
    )
    GMAIL_TOKEN_FILE = os.getenv(
        "GMAIL_TOKEN_FILE",
        "gmail_token.json",
    )
    GMAIL_QUERY = os.getenv(
        "GMAIL_QUERY",
        "from:noreply@transfernow.net subject:TransferNow",
    )
    GMAIL_MAX_MESSAGES = _bounded_int(
        os.getenv("GMAIL_MAX_MESSAGES"),
        default=5,
        maximum=5,
    )
