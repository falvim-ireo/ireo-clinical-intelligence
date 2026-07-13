import os
from dotenv import load_dotenv

load_dotenv()


def _bounded_int(value: str | None, default: int, maximum: int) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, 1), maximum)


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
        r"C:\Program Files\WinRAR\WinRAR.exe",
    )
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
