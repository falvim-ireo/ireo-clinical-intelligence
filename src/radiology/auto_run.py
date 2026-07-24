"""Execução agendada, não interativa e fail-closed do Radiology Intake."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import linecache
from pathlib import Path
import re
import sqlite3
import traceback
from typing import Callable
from uuid import uuid4

from observability.audit_logger import AuditLogger

from integrations.transfernow_connector import TransferNowConnector
from models.imaging_exam import ImagingExam
from models.resolved_patient import ResolutionReason
from radiology.intake_history import IntakeHistoryError, fingerprint
from radiology.supervised_import import ConfirmedPatient
from radiology.transfernow_download import (
    BrowserInteractionRequired, DownloadResult, TransferNowDownloader,
)
from repositories.patient_repository import PatientRepositoryUnavailableError, InMemoryPatientRepository
from services.patient_resolver import PatientResolver


@dataclass
class AutoRunSummary:
    started_at: str
    run_id: str = ""
    finished_at: str = ""
    messages_found: int = 0
    completed: int = 0
    review_required: int = 0
    duplicates_blocked: int = 0
    failed: int = 0
    correlation_ids: list[str] | None = None
    reason_codes: list[str] | None = None
    headless_download_completed: int = 0
    headless_download_review_required: int = 0
    copied_automatically: int = 0
    skipped_completed: int = 0
    skipped_already_registered: int = 0
    duplicate_blocked: int = 0
    duration_seconds: float = 0.0
    diagnostics: list[dict[str, str]] | None = None
    exit_code: int = 0
    global_failure: dict[str, str] | None = None

    def __post_init__(self):
        self.correlation_ids = self.correlation_ids or []
        self.reason_codes = self.reason_codes or []
        self.diagnostics = self.diagnostics or []


def new_summary(now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> AutoRunSummary:
    return AutoRunSummary(started_at=now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"), run_id=uuid4().hex)


def sanitized_failure(exc: Exception, stage: str, reason_code: str, *, debug: bool = False) -> dict[str, str]:
    result = {"stage": stage, "exception_type": type(exc).__name__[:64],
            "reason_code": reason_code, "sanitized_message": RadiologyAutoRunner._sanitize_exception(exc)}
    if debug:
        frame = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
        if frame:
            result.update({"function": frame.name[:64], "line": str(frame.lineno),
                "operation": _safe_operation(frame.filename, frame.lineno),
                "argument_types": _frame_argument_types(exc.__traceback__)})
    return result


def sanitized_history_failure(exc: IntakeHistoryError) -> dict[str, str]:
    original = exc.__cause__ or exc
    frame = traceback.extract_tb(original.__traceback__)[-1] if original.__traceback__ else None
    sqlite_code = getattr(original, "sqlite_errorcode", None)
    return {
        "function": frame.name[:64] if frame else "unknown",
        "line": str(frame.lineno) if frame else "0",
        "sqlite_operation": getattr(exc, "operation", "HISTORY")[:64],
        "original_exception_type": type(original).__name__[:64],
        "sqlite_code": str(sqlite_code) if sqlite_code is not None else "unavailable",
        "sanitized_original_message": _sanitize_sqlite_message(original),
    }


def _sanitize_sqlite_message(exc: Exception) -> str:
    message = str(exc).strip()
    safe_patterns = (
        r"database is (?:locked|busy)",
        r"no such (?:table|column): [A-Za-z_][A-Za-z0-9_]*",
        r"table [A-Za-z_][A-Za-z0-9_]* has no column named [A-Za-z_][A-Za-z0-9_]*",
        r"datatype mismatch",
        r"attempt to write a readonly database",
        r"database disk image is malformed",
        r"unable to open database file",
    )
    for pattern in safe_patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            return match.group(0)[:160]
    if isinstance(exc, sqlite3.Error):
        return "SQLite operation failed."
    return "History validation failed."


def _safe_operation(filename: str, lineno: int) -> str:
    source = linecache.getline(filename, lineno).strip()
    source = re.sub(r"(['\"]).*?\1", "<value>", source)
    return source[:120] if source and all(char.isalnum() or char in " _.,()[]=:+-*/<>" for char in source) else "internal operation"


def _frame_argument_types(traceback_object) -> str:
    current = traceback_object
    while current and current.tb_next:
        current = current.tb_next
    if not current:
        return "unavailable"
    types = sorted({type(value).__name__ for value in current.tb_frame.f_locals.values()})
    return ", ".join(types[:8]) or "unavailable"


def write_summary_atomic(summary: AutoRunSummary, path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(asdict(summary), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)


class _DownloadCheckpoint:
    """Marcador sanitizado para retomar após falha do histórico."""

    def __init__(self, quarantine_root: str | Path, message_id: str) -> None:
        self.root = Path(quarantine_root).expanduser().resolve()
        message_hash = hashlib.sha256(message_id.encode()).hexdigest()
        self.path = self.root / ".auto-run-state" / f"{message_hash}.json"
        self.message_hash = message_hash

    def mark_pending(self, correlation_id: str) -> None:
        self._write({"status": "PENDING", "correlation_id": correlation_id})

    def mark_completed(self, result: DownloadResult, correlation_id: str) -> None:
        self._write({
            "status": "COMPLETED", "correlation_id": correlation_id,
            "size_bytes": result.size_bytes, "sha256": result.sha256,
        })

    def load(self, previous_record=None) -> DownloadResult | None:
        data = self._read()
        if data is None and previous_record is not None and previous_record.archive_sha256:
            data = {
                "status": "COMPLETED",
                "correlation_id": previous_record.correlation_id,
                "size_bytes": previous_record.archive_size,
                "sha256": previous_record.archive_sha256,
            }
        if not data or data.get("status") != "COMPLETED":
            return None
        correlation_id = data.get("correlation_id")
        digest = data.get("sha256")
        size = data.get("size_bytes")
        if not isinstance(correlation_id, str) or not re.fullmatch(r"[a-zA-Z0-9-]{8,64}", correlation_id):
            return None
        if not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
            return None
        if not isinstance(size, int) or size <= 0:
            return None
        folder = self.root / correlation_id
        if not folder.resolve().is_relative_to(self.root) or not folder.is_dir():
            return None
        candidates = [
            item for item in folder.iterdir()
            if item.is_file() and item.suffix.casefold() in TransferNowDownloader.EXTENSIONS
        ]
        if len(candidates) != 1 or candidates[0].stat().st_size != size:
            return None
        actual = self._sha256(candidates[0])
        if actual.casefold() != digest.casefold():
            return None
        self.mark_completed(DownloadResult(candidates[0], size, actual, True), correlation_id)
        return DownloadResult(candidates[0], size, actual, True)

    def _read(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if data.get("message_id_sha256") == self.message_hash else None
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _write(self, values: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"message_id_sha256": self.message_hash, **values}
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(self.path)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


class RadiologyAutoRunner:
    """Coordena somente decisões inequívocas; toda dúvida fica em revisão."""

    def __init__(self, *, gmail, downloader, importer, history, enabled: bool,
                 allow_copy: bool, max_messages: int, browser_mode: str = "review", browser_downloader=None,
                 require_exact_match: bool = True, summary_path="data/last_auto_run_summary.json",
                 review_report_path="data/radiology_review_required.txt", start_date: str = "",
                 debug: bool = False,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.gmail, self.downloader, self.importer, self.history = gmail, downloader, importer, history
        self.enabled, self.allow_copy = enabled, allow_copy
        self.max_messages = min(max(int(max_messages), 1), 20)
        self.browser_mode = (
            browser_mode if browser_mode in {"headless", "review", "visible"}
            else "review"
        )
        self.browser_downloader = browser_downloader
        if self.browser_downloader is not None and self.browser_mode != "review":
            self.browser_downloader.headless = self.browser_mode == "headless"
            self.browser_downloader.allow_manual_interaction = False
        self.require_exact_match = require_exact_match
        self.summary_path, self.now = Path(summary_path), now
        self.review_report_path = Path(review_report_path)
        self.debug = bool(debug)
        try: self.start_date = date.fromisoformat(start_date) if start_date else None
        except ValueError: self.start_date = None

    def run(self) -> tuple[int, AutoRunSummary]:
        summary = new_summary(self.now)
        code = 0
        try:
            if not self.enabled:
                return code, summary
            messages = self.gmail.list_messages(
                query="from:noreply@transfernow.net subject:TransferNow",
                max_results=self.max_messages,
            )
            summary.messages_found = len(messages)
            for message in messages:
                if self.start_date and message.received_at.date() < self.start_date:
                    summary.skipped_completed += 1
                    summary.reason_codes.append("BEFORE_START_DATE")
                    continue
                self._process(message, summary)
            code = 1 if summary.failed else 0
        except Exception as exc:
            summary.failed += 1
            summary.reason_codes.append("GLOBAL_AUTO_RUN_FAILURE")
            summary.global_failure = sanitized_failure(exc, "AUTO_RUN", "GLOBAL_AUTO_RUN_FAILURE", debug=self.debug)
            code = 1
        finally:
            self._finish(summary, code)
        return code, summary

    def _process(self, message, summary: AutoRunSummary) -> None:
        correlation_id = uuid4().hex
        self.importer.audit_logger = AuditLogger(correlation_id=correlation_id)
        summary.correlation_ids.append(f"correlation-{fingerprint(correlation_id)[:12]}")
        record_id = None
        try:
            body = "\n".join(x for x in (message.text_body, message.html_body) if x)
            transfer = TransferNowConnector.interpretar(body, message.subject)
            if not transfer.original_filename or not transfer.patient_name_candidate:
                return self._review(None, summary, "INVALID_MESSAGE", "PARSE", message.message_id, correlation_id)
            previous_record = self.history.find_latest_by_message(message.message_id)
            duplicate = self.history.find_duplicate(gmail_message_id=message.message_id)
            if duplicate:
                summary.duplicates_blocked += 1
                summary.duplicate_blocked += 1
                summary.reason_codes.append("DUPLICATE_MESSAGE")
                return
            checkpoint = _DownloadCheckpoint(self.importer.quarantine_root, message.message_id)
            downloaded = checkpoint.load(previous_record)
            if downloaded is not None and previous_record is not None:
                if previous_record.archive_sha256 == downloaded.sha256:
                    record_id = previous_record.id
            if downloaded is None:
                checkpoint.mark_pending(correlation_id)
                try:
                    downloaded = self.downloader.download(transfer.download_url, transfer.original_filename,
                                                          self.importer.quarantine_root, correlation_id, message.message_id)
                except BrowserInteractionRequired:
                    if self.browser_mode == "review" or self.browser_downloader is None:
                        return self._review(None, summary, "BROWSER_INTERACTION_REQUIRED", "DOWNLOAD", message.message_id, correlation_id)
                    try:
                        downloaded = self.browser_downloader.download(transfer.download_url, transfer.original_filename,
                            self.importer.quarantine_root, correlation_id, message.message_id)
                        summary.headless_download_completed += 1
                    except Exception:
                        summary.headless_download_review_required += 1
                        reason = (
                            "BROWSER_AUTOMATION_FAILED"
                            if self.browser_mode == "visible"
                            else "HEADLESS_DOWNLOAD_FAILED"
                        )
                        return self._review(None, summary, reason, "DOWNLOAD", message.message_id, correlation_id)
                checkpoint.mark_completed(downloaded, correlation_id)
            if record_id is None:
                record_id = self.importer.register_download(archive_path=downloaded.path, archive_sha256=downloaded.sha256,
                                                            gmail_message_id=message.message_id, transfer_url=transfer.download_url)
            self.history.update(record_id, "DOWNLOADED", run_id=summary.run_id)
            if self.history.find_duplicate(archive_sha256=downloaded.sha256, gmail_message_id=message.message_id):
                # O registro recém-criado não é COMPLETED; este ramo representa histórico prévio.
                match = self.history.find_duplicate(archive_sha256=downloaded.sha256)
                if match and match.record.id != record_id:
                    return self._review(record_id, summary, "DUPLICATE_ARCHIVE", "DUPLICATE")
            extracted = self.importer.extractor.extract(downloaded.path)
            self.history.update(record_id, "EXTRACTED")
            patient = self._resolve_patient(transfer.patient_name_candidate)
            payload_files = self.importer._source_files(extracted)
            dicom_analysis = None
            if getattr(self.importer, "dicom_reader", None) is not None:
                dicom_analysis = self.importer.dicom_reader.analyze(
                    extracted,
                    confirmed_patient_name=patient.name,
                    confirmed_patient_id=patient.patient_id,
                )
                self.importer.dicom_reader.write_reports(dicom_analysis, extracted)
                if dicom_analysis.requires_manual_review:
                    return self._review(
                        record_id,
                        summary,
                        "MULTIPLE_DICOM_PATIENTS",
                        "DICOM_VALIDATION",
                    )
            folder = self._resolve_folder(patient)
            files = self.importer._source_files(extracted)
            clinical_date = None
            if dicom_analysis is not None and hasattr(
                self.importer, "_resolve_clinical_exam_date"
            ):
                clinical_date = self.importer._resolve_clinical_exam_date(
                    dicom_analysis=dicom_analysis,
                    archive_name=downloaded.path.name,
                    sender_exam_date=None,
                    sender_exam_time=None,
                    email_received_at=message.received_at,
                    import_started_at=self.importer._utc_datetime(
                        self.importer.now_provider()
                    ),
                )
            destination = (
                self.importer._next_destination(folder, clinical_date.exam_date)
                if clinical_date is not None
                else self.importer._next_destination(folder)
            )
            possible = self.history.find_duplicate(archive_filename=downloaded.path.name,
                                                   archive_size=downloaded.size_bytes,
                                                   patient_id=patient.patient_id, destination=destination)
            if possible:
                return self._review(record_id, summary, "DUPLICATE_POSSIBLE", "DUPLICATE")
            self.history.update(record_id, "READY_FOR_CONFIRMATION", patient_id_hash=fingerprint(patient.patient_id),
                                destination_fingerprint=fingerprint(destination), file_count=len(payload_files),
                                total_size=sum(x.stat().st_size for x in payload_files))
            if not self.allow_copy:
                return self._review(record_id, summary, "AUTO_COPY_DISABLED", "COPY")
            result = self.importer._copy_and_manifest(archive=downloaded.path, extracted=extracted, destination=destination,
                patient=patient, files=files, total_size=sum(x.stat().st_size for x in payload_files), source="gmail-auto",
                folder_selection_mode="auto", folder_selection_reason="SINGLE_COMPATIBLE_FOLDER",
                dicom_analysis=dicom_analysis, clinical_date=clinical_date,
                payload_file_count=len(payload_files),
                intake_record={"correlation_id": correlation_id, "archive_sha256": downloaded.sha256,
                               "duplicate_check": "clear", "reimport": False, "previous_record_reference": None})
            self.history.update(record_id, "COMPLETED", manifest_path_fingerprint=fingerprint(result.manifest_path),
                                file_checksums_fingerprint=fingerprint(json.dumps(result.checksums, sort_keys=True)))
            summary.completed += 1
            summary.copied_automatically += 1
        except PatientRepositoryUnavailableError:
            self._review(record_id, summary, "CLINICORP_UNAVAILABLE", "PATIENT_RESOLUTION")
        except IntakeHistoryError as exc:
            self._failed(record_id, summary, "HISTORY_UNAVAILABLE", "HISTORY", exc, message.message_id, correlation_id)
        except ValueError as exc:
            if str(exc) in {"PATIENT_REVIEW_REQUIRED", "FOLDER_REVIEW_REQUIRED"}:
                self._review(record_id, summary, str(exc), "SELECTION")
            else:
                self._failed(record_id, summary, "UNEXPECTED_ERROR", "SELECTION", exc, message.message_id, correlation_id)
        except Exception as exc:
            self._failed(record_id, summary, "UNEXPECTED_ERROR", "AUTO_RUN", exc, message.message_id, correlation_id)

    def _resolve_patient(self, name: str) -> ConfirmedPatient:
        candidates = list(self.importer.patient_repository.find_candidates(name))
        resolved = PatientResolver(InMemoryPatientRepository(candidates), self.importer.audit_logger).resolve(ImagingExam(patient_name=name))
        allowed = {ResolutionReason.EXACT_NAME, ResolutionReason.NORMALIZED_NAME}
        if not (resolved.matched and resolved.patient_id is not None and resolved.candidate_count == 1
                and not resolved.requires_manual_review and resolved.confidence_score >= .98
                and (not self.require_exact_match or resolved.resolution_reason in allowed)):
            raise ValueError("PATIENT_REVIEW_REQUIRED")
        item = next(x for x in candidates if x.id == resolved.patient_id)
        return ConfirmedPatient(item.nome, item.id, "auto", resolved.resolution_reason.value,
                                resolved.confidence_score, resolved.candidate_count)

    def _resolve_folder(self, patient: ConfirmedPatient):
        folder, mode, _ = self.importer._choose_patient_folder(
            patient.name, allow_auto=True
        )
        if mode != "auto":
            raise ValueError("FOLDER_REVIEW_REQUIRED")
        return folder

    def _review(self, record_id, summary, reason, stage, gmail_message_id=None, correlation_id=None):
        if record_id is None and gmail_message_id and correlation_id:
            try:
                record_id = self.history.create_review(correlation_id=correlation_id,
                    gmail_message_id=gmail_message_id, reason_code=reason, stage=stage,
                    run_id=summary.run_id).id
            except IntakeHistoryError:
                summary.failed += 1
                summary.reason_codes.append("HISTORY_UNAVAILABLE")
                return
        if record_id is not None:
            try:
                _, already_registered = self.history.set_review_required(
                    record_id, reason_code=reason, stage=stage, run_id=summary.run_id
                )
            except IntakeHistoryError:
                summary.failed += 1; summary.reason_codes.append("HISTORY_UNAVAILABLE"); return
            if already_registered:
                summary.skipped_already_registered += 1
            else:
                summary.review_required += 1
            summary.reason_codes.append(reason)

    def _failed(self, record_id, summary, reason, stage, exc, gmail_message_id, correlation_id):
        safe_type = type(exc).__name__[:64]
        if record_id is None:
            try:
                record_id = self.history.create_review(correlation_id=correlation_id,
                    gmail_message_id=gmail_message_id, reason_code=reason, stage=stage,
                    run_id=summary.run_id).id
            except IntakeHistoryError:
                record_id = None
        if record_id is not None:
            try: self.history.update(record_id, "FAILED", reason_code=reason, stage=stage, exception_type=safe_type, run_id=summary.run_id)
            except IntakeHistoryError: pass
        summary.failed += 1; summary.reason_codes.append(reason)
        if self.debug:
            diagnostic = {"correlation_id": f"correlation-{fingerprint(correlation_id)[:12]}",
                "stage": stage, "exception_type": safe_type, "internal_reason_code": reason,
                "message": self._sanitize_exception(exc)}
            if isinstance(exc, IntakeHistoryError):
                diagnostic.update(sanitized_history_failure(exc))
            summary.diagnostics.append(diagnostic)

    @staticmethod
    def _sanitize_exception(exc):
        # A mensagem original pode conter caminho, URL ou dado clínico; nunca a propague.
        return f"technical failure ({type(exc).__name__[:64]})"

    def _finish(self, summary, code):
        summary.finished_at = self._timestamp()
        summary.duration_seconds = max(0.0, (datetime.fromisoformat(summary.finished_at.replace("Z", "+00:00")) - datetime.fromisoformat(summary.started_at.replace("Z", "+00:00"))).total_seconds())
        summary.exit_code = code
        write_summary_atomic(summary, self.summary_path)
        self._write_review_report()
        return code, summary

    def _timestamp(self): return self.now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _write_review_report(self):
        try:
            records = self.history.list_records(limit=100, status="REVIEW_REQUIRED")
            lines = ["Pendências radiológicas — dados sanitizados"]
            for record in records:
                lines.append(" | ".join((record.created_at_utc, record.safe_reference, record.stage or "UNKNOWN", record.reason_code or "UNKNOWN", "Retome pelo fluxo supervisionado.")))
            self.review_report_path.parent.mkdir(parents=True, exist_ok=True)
            self.review_report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except (OSError, IntakeHistoryError):
            pass
