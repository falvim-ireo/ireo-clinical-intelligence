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
    IREO_RADIOLOGY_INDEX_DATABASE_PATH = os.getenv(
        "IREO_RADIOLOGY_INDEX_DATABASE_PATH", "data/ireo_radiology_index.db"
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
    CFAZ_GMAIL_QUERY = os.getenv(
        "CFAZ_GMAIL_QUERY", "from:(cfaz.net) newer_than:30d"
    )
    CFAZ_GMAIL_MAX_MESSAGES = _bounded_int(
        os.getenv("CFAZ_GMAIL_MAX_MESSAGES"), 100, 500
    )
    CFAZ_API_TOKEN = os.getenv("CFAZ_API_TOKEN", "").strip()
    CFAZ_EMAIL = os.getenv("CFAZ_EMAIL", "").strip()
    CFAZ_PASSWORD = os.getenv("CFAZ_PASSWORD", "")
    CFAZ_API_TIMEOUT_SECONDS = _bounded_int(
        os.getenv("CFAZ_API_TIMEOUT_SECONDS"), 30, 300
    )
    CFAZ_MAX_FILE_BYTES = _bounded_int(
        os.getenv("CFAZ_MAX_FILE_BYTES"), 2 * 1024 * 1024 * 1024,
        20 * 1024 * 1024 * 1024,
    )
    IREO_RADIOLOGY_STORAGE_PATH = os.getenv(
        "IREO_RADIOLOGY_STORAGE_PATH", "data/radiology"
    )
    MS_GRAPH_CLIENT_ID = os.getenv("MS_GRAPH_CLIENT_ID", "").strip()
    MS_GRAPH_AUTHORITY = os.getenv("MS_GRAPH_AUTHORITY", "").strip()
    MS_GRAPH_SCOPES = os.getenv("MS_GRAPH_SCOPES", "").strip()
    MS_GRAPH_TOKEN_CACHE_FILE = os.getenv(
        "MS_GRAPH_TOKEN_CACHE_FILE", "data/ms_graph_token_cache.json"
    ).strip()
    MS_GRAPH_ONEDRIVE_ROOT = os.getenv("MS_GRAPH_ONEDRIVE_ROOT", "").strip()
    MS_GRAPH_UPLOAD_CHUNK_SIZE_BYTES = _bounded_int(
        os.getenv("MS_GRAPH_UPLOAD_CHUNK_SIZE_BYTES"),
        10 * 1024 * 1024,
        60 * 1024 * 1024,
    )
    MS_GRAPH_READ_TIMEOUT_SECONDS = _bounded_int(
        os.getenv("MS_GRAPH_READ_TIMEOUT_SECONDS"), 30, 300
    )
    IREO_REBUILD_HEARTBEAT_SECONDS = _bounded_float(
        os.getenv("IREO_REBUILD_HEARTBEAT_SECONDS"), 5.0, 1.0, 30.0
    )
    IREO_REBUILD_MAX_ATTEMPTS = _bounded_int(
        os.getenv("IREO_REBUILD_MAX_ATTEMPTS"), 5, 10
    )
