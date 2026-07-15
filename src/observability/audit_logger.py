"""Eventos estruturados com uma allowlist estrita de dados operacionais."""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import logging
from pathlib import PurePath
import re
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit
from uuid import uuid4


class AuditEventType(str, Enum):
    """Tipos de evento permitidos no piloto de Radiology Intake."""

    RADIOLOGY_EMAIL_DETECTED = "RADIOLOGY_EMAIL_DETECTED"
    TRANSFERNOW_MESSAGE_PARSED = "TRANSFERNOW_MESSAGE_PARSED"
    PATIENT_RESOLUTION_STARTED = "PATIENT_RESOLUTION_STARTED"
    PATIENT_RESOLUTION_MATCHED = "PATIENT_RESOLUTION_MATCHED"
    PATIENT_RESOLUTION_REVIEW_REQUIRED = (
        "PATIENT_RESOLUTION_REVIEW_REQUIRED"
    )
    PATIENT_SOURCE_UNAVAILABLE = "PATIENT_SOURCE_UNAVAILABLE"
    RADIOLOGY_DRY_RUN_COMPLETED = "RADIOLOGY_DRY_RUN_COMPLETED"
    RADIOLOGY_DRY_RUN_FAILED = "RADIOLOGY_DRY_RUN_FAILED"
    PATIENT_AUTO_SELECTED = "PATIENT_AUTO_SELECTED"
    PATIENT_MANUAL_SELECTION_REQUIRED = "PATIENT_MANUAL_SELECTION_REQUIRED"
    ONEDRIVE_FOLDER_AUTO_SELECTED = "ONEDRIVE_FOLDER_AUTO_SELECTED"
    ONEDRIVE_FOLDER_MANUAL_SELECTION_REQUIRED = "ONEDRIVE_FOLDER_MANUAL_SELECTION_REQUIRED"
    INTAKE_RECORD_CREATED = "INTAKE_RECORD_CREATED"
    DUPLICATE_CHECK_STARTED = "DUPLICATE_CHECK_STARTED"
    DUPLICATE_NOT_FOUND = "DUPLICATE_NOT_FOUND"
    DUPLICATE_POSSIBLE = "DUPLICATE_POSSIBLE"
    DUPLICATE_CONFIRMED = "DUPLICATE_CONFIRMED"
    REIMPORT_REQUESTED = "REIMPORT_REQUESTED"
    REIMPORT_CONFIRMED = "REIMPORT_CONFIRMED"
    IMPORT_HISTORY_WRITE_FAILED = "IMPORT_HISTORY_WRITE_FAILED"


@dataclass(frozen=True)
class AuditEvent:
    """Registro operacional que não contém conteúdo clínico identificável."""

    event_type: AuditEventType
    timestamp: datetime
    correlation_id: str
    status: str
    masked_message_id: Optional[str] = None
    masked_patient_id: Optional[str] = None
    requires_manual_review: Optional[bool] = None
    reason_code: Optional[str] = None
    metadata: dict[str, str | int | float | bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("AuditEvent.timestamp deve conter timezone.")
        if self.timestamp.utcoffset() != timezone.utc.utcoffset(self.timestamp):
            raise ValueError("AuditEvent.timestamp deve estar em UTC.")

    def to_dict(self) -> dict[str, Any]:
        """Converte o evento para uma estrutura serializável em JSON."""

        payload = asdict(self)
        payload["event_type"] = self.event_type.value
        payload["timestamp"] = self.timestamp.isoformat().replace(
            "+00:00",
            "Z",
        )
        return payload


def _fingerprint(prefix: str, value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def mask_message_id(message_id: Any) -> Optional[str]:
    """Cria uma referência determinística não reversível para a mensagem."""

    return _fingerprint("msg", message_id)


def mask_patient_id(patient_id: Any) -> Optional[str]:
    """Cria uma referência determinística não reversível para o paciente."""

    return _fingerprint("patient", patient_id)


def mask_archive_name(archive_name: Any) -> Optional[str]:
    """Substitui o nome por fingerprint, preservando só extensão segura."""

    text = str(archive_name or "").strip()
    fingerprint = _fingerprint("archive", text)
    if fingerprint is None:
        return None
    extension = PurePath(text).suffix.casefold()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", extension):
        extension = ""
    return f"{fingerprint}{extension}"


def safe_hostname(url: Any) -> Optional[str]:
    """Retorna somente um hostname ASCII simples, nunca path ou query."""

    try:
        hostname = (urlsplit(str(url or "")).hostname or "").casefold()
    except (TypeError, ValueError):
        return None
    if not re.fullmatch(r"[a-z0-9.-]+", hostname):
        return None
    return hostname or None


_ALLOWED_MATCH_METHODS = {
    "exact_name",
    "normalized_name",
    "similarity_score",
}
_ALLOWED_PATIENT_SOURCES = {"clinicorp", "offline"}


def _sanitize_metadata(
    metadata: Optional[Mapping[str, Any]],
    download_url: Any,
) -> dict[str, str | int | float | bool]:
    sanitized: dict[str, str | int | float | bool] = {}
    supplied = metadata or {}

    candidate_count = supplied.get("candidate_count")
    if isinstance(candidate_count, int) and candidate_count >= 0:
        sanitized["candidate_count"] = candidate_count

    folder_candidate_count = supplied.get("folder_candidate_count")
    if isinstance(folder_candidate_count, int) and folder_candidate_count >= 0:
        sanitized["folder_candidate_count"] = folder_candidate_count

    confidence_score = supplied.get("confidence_score")
    if isinstance(confidence_score, (int, float)) and 0 <= confidence_score <= 1:
        sanitized["confidence_score"] = round(float(confidence_score), 4)

    override_manual = supplied.get("override_manual")
    if isinstance(override_manual, bool):
        sanitized["override_manual"] = override_manual

    matched_by = supplied.get("matched_by")
    if matched_by in _ALLOWED_MATCH_METHODS:
        sanitized["matched_by"] = str(matched_by)

    patient_source = supplied.get("patient_source")
    if patient_source in _ALLOWED_PATIENT_SOURCES:
        sanitized["patient_source"] = str(patient_source)

    message_count = supplied.get("message_count")
    if isinstance(message_count, int) and message_count >= 0:
        sanitized["message_count"] = message_count

    hostname = safe_hostname(download_url)
    if hostname:
        sanitized["archive_host"] = hostname

    return sanitized


def _safe_code(value: Any) -> Optional[str]:
    code = str(getattr(value, "value", value) or "").strip().upper()
    if re.fullmatch(r"[A-Z0-9_]{1,64}", code):
        return code
    return None


def _safe_correlation_id(value: Any) -> str:
    candidate = str(value or "").strip()
    if re.fullmatch(r"[a-zA-Z0-9-]{8,64}", candidate):
        return candidate
    return uuid4().hex


def parse_log_level(value: Any) -> int:
    """Aceita somente os níveis autorizados para esta fase."""

    levels = {
        "WARNING": logging.WARNING,
        "INFO": logging.INFO,
        "DEBUG": logging.DEBUG,
    }
    return levels.get(str(value or "").strip().upper(), logging.WARNING)


class AuditLogger:
    """Emite JSON sanitizado e nunca propaga falhas ao fluxo principal."""

    _WARNING_EVENTS = {
        AuditEventType.PATIENT_SOURCE_UNAVAILABLE,
        AuditEventType.RADIOLOGY_DRY_RUN_FAILED,
    }

    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        level: Any = "WARNING",
        correlation_id: Any = None,
    ) -> None:
        self.correlation_id = _safe_correlation_id(correlation_id)
        self._logger = logger or self._default_logger()
        self._logger.setLevel(parse_log_level(level))

    def _default_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"ireo.audit.{self.correlation_id}")
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
        logger.propagate = False
        return logger

    def emit(
        self,
        event_type: AuditEventType,
        *,
        status: Any,
        message_id: Any = None,
        patient_id: Any = None,
        archive_name: Any = None,
        download_url: Any = None,
        requires_manual_review: Optional[bool] = None,
        reason_code: Any = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Optional[AuditEvent]:
        """Registra um evento; dados fora da allowlist são descartados."""

        try:
            event = AuditEvent(
                event_type=AuditEventType(event_type),
                timestamp=datetime.now(timezone.utc),
                correlation_id=self.correlation_id,
                status=_safe_code(status) or "UNKNOWN",
                masked_message_id=mask_message_id(message_id),
                masked_patient_id=mask_patient_id(patient_id),
                requires_manual_review=(
                    requires_manual_review
                    if isinstance(requires_manual_review, bool)
                    else None
                ),
                reason_code=_safe_code(reason_code),
                metadata={
                    **_sanitize_metadata(metadata, download_url),
                    **(
                        {"masked_archive_name": masked_archive}
                        if (masked_archive := mask_archive_name(archive_name))
                        else {}
                    ),
                },
            )
            level = (
                logging.WARNING
                if event.event_type in self._WARNING_EVENTS
                else logging.INFO
            )
            self._logger.log(
                level,
                json.dumps(
                    event.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            return event
        except Exception:
            return None


def emit_safely(audit_logger: Any, event_type: AuditEventType, **fields) -> None:
    """Protege o workflow até contra implementações injetadas defeituosas."""

    try:
        if audit_logger is not None:
            audit_logger.emit(event_type, **fields)
    except Exception:
        return
