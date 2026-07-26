"""MVP interativo de importação radiológica estritamente supervisionada."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
from time import monotonic
from typing import Any, Optional
from urllib.parse import urlsplit

import requests

from integrations.gmail_connector import GmailConnector
from integrations.onedrive_graph import (
    GraphFolder,
    OneDriveFolderConflictError,
    OneDriveGraphClient,
    OneDriveGraphError,
)
from integrations.transfernow_connector import TransferNowConnector
from models.imaging_exam import ImagingExam
from models.patient import Patient
from models.resolved_patient import ResolvedPatient, ResolutionReason
from observability.audit_logger import AuditEventType, AuditLogger, emit_safely, mask_patient_id
from radiology.archive_extractor import ArchiveExtractor
from radiology.dicom_reader import DicomPackageAnalysis, DicomReader
from radiology.exam_index_service import ExamIndexError, ExamIndexService
from radiology.intake_history import (
    DuplicateMatch,
    IntakeHistoryError,
    IntakeHistoryRepository,
    fingerprint,
)
from repositories.patient_repository import (
    PatientRepository,
    PatientRepositoryUnavailableError,
    InMemoryPatientRepository,
)
from services.patient_resolver import PatientResolver
from services.patient_normalizer import PatientNormalizer


LOGGER = logging.getLogger(__name__)


class SupervisedImportError(RuntimeError):
    """Erro seguro que impede a importação supervisionada."""


class SupervisedImportCancelled(SupervisedImportError):
    """O operador não forneceu a confirmação explícita exigida."""


@dataclass(frozen=True)
class ConfirmedPatient:
    name: str
    patient_id: Optional[int]
    selection_mode: str = "manual"
    selection_reason: str = "MANUAL_SELECTION"
    confidence_score: float = 0.0
    candidate_count: int = 0


@dataclass(frozen=True)
class ClinicalExamDate:
    exam_date: date
    exam_time: time | None
    exam_date_source: str
    email_received_at: datetime | None
    import_started_at: datetime


@dataclass(frozen=True)
class SupervisedImportResult:
    destination: Path
    manifest_path: Path
    onedrive_destination: str
    file_count: int
    total_size_bytes: int
    checksums: dict[str, str]


class SupervisedRadiologyImporter:
    @staticmethod
    def _preserve_structured_tomography(extracted_root: Path, provider: str) -> None:
        """Mantém pacotes TransferNow proprietários com sua estrutura relativa."""
        if str(provider or "").casefold() != "transfernow":
            return
        root = Path(extracted_root)
        package = root / "03 - Tomografia" / "Pacote Original"
        package.mkdir(parents=True, exist_ok=True)
        for child in list(root.iterdir()):
            if child.name == "03 - Tomografia":
                continue
            shutil.move(str(child), str(package / child.name))

    """Executa aquisição, revisão e cópia somente após confirmação humana."""

    def __init__(
        self,
        *,
        quarantine_root: str | Path,
        archive_tool_path: str | Path,
        onedrive_client: OneDriveGraphClient,
        onedrive_root: str,
        archive_timeout_seconds: int = 1800,
        patient_repository: PatientRepository,
        gmail_connector: Optional[GmailConnector] = None,
        audit_logger: Optional[AuditLogger] = None,
        input_func: Callable[[str], str] = input,
        output: Callable[[str], None] = print,
        downloader: Optional[Callable[[str, str, Path], Path]] = None,
        today_provider: Callable[[], date] = date.today,
        now_provider: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        auto_select_unambiguous: bool = False,
        auto_select_min_score: float = 0.95,
        force_manual_selection: bool = False,
        patient_source_mode: str = "clinicorp",
        intake_history: IntakeHistoryRepository | None = None,
        allow_reimport: bool = False,
        progress_output: Callable[[str], None] | None = None,
        monotonic_provider: Callable[[], float] = monotonic,
        dicom_reader: DicomReader | None = None,
        exam_index_service: ExamIndexService | None = None,
    ) -> None:
        self.quarantine_root = Path(quarantine_root).expanduser().resolve()
        self.staging_root = (self.quarantine_root / "supervised-staging").resolve()
        self.onedrive_client = onedrive_client
        self.onedrive_root = onedrive_root.strip().strip("/\\")
        if not self.onedrive_root:
            raise ValueError("A raiz remota do OneDrive não foi configurada.")
        self.patient_repository = patient_repository
        self.gmail_connector = gmail_connector
        self.audit_logger = audit_logger or AuditLogger()
        self.input = input_func
        self.output = output
        self.progress_output = progress_output or (
            (lambda message: print(f"\r{message}", end="", flush=True))
            if output is print
            else output
        )
        self.monotonic_provider = monotonic_provider
        self.dicom_reader = dicom_reader or DicomReader()
        self.exam_index_service = exam_index_service
        self.downloader = downloader or self._download_transfernow
        self.today_provider = today_provider
        self.now_provider = now_provider
        self.auto_select_unambiguous = bool(auto_select_unambiguous)
        self.auto_select_min_score = min(max(float(auto_select_min_score), 0.0), 1.0)
        self.force_manual_selection = bool(force_manual_selection)
        self.patient_source_mode = patient_source_mode
        self.intake_history = intake_history
        self.allow_reimport = bool(allow_reimport)
        self._approved_duplicate_records: set[int] = set()
        self.extractor = ArchiveExtractor(
            self.quarantine_root,
            archive_tool_path,
            timeout_seconds=archive_timeout_seconds,
        )

    def run(
        self,
        *,
        archive_path: str | Path | None = None,
        email_message_id: str | None = None,
        gmail_message_id: str | None = None,
        transfer_url: str | None = None,
        archive_sha256: str | None = None,
        intake_record_id: int | None = None,
        email_received_at: datetime | None = None,
        sender_exam_date: date | str | None = None,
        sender_exam_time: time | str | None = None,
        acquisition_metadata: dict[str, Any] | None = None,
        acquisition_exam_id: str | None = None,
        source_provider: str | None = None,
    ) -> SupervisedImportResult:
        if bool(archive_path) == bool(email_message_id):
            raise SupervisedImportError(
                "Informe exatamente uma entrada: e-mail ou arquivo local."
            )

        record_id: int | None = intake_record_id
        duplicate_state = "clear"
        previous_reference: str | None = None
        reimport = False
        import_started_at = self._utc_datetime(self.now_provider())
        try:
            archive, probable_name, source, acquired_email_received_at = self._acquire_archive(
                archive_path, email_message_id,
            )
            email_received_at = email_received_at or acquired_email_received_at
            source = source_provider or source
            self.output("Download.............. OK")
            archive_sha256 = archive_sha256 or self._file_sha256(archive)
            if self.intake_history and record_id is None:
                record = self.intake_history.create_downloaded(
                    correlation_id=self.audit_logger.correlation_id,
                    archive_filename=archive.name,
                    archive_size=archive.stat().st_size,
                    archive_sha256=archive_sha256,
                    gmail_message_id=gmail_message_id or email_message_id,
                    transfer_url=transfer_url,
                )
                record_id = record.id
                self._emit_history(AuditEventType.INTAKE_RECORD_CREATED, "DOWNLOADED")
                match = self._check_duplicate(
                    archive_sha256=archive_sha256,
                    gmail_message_id=gmail_message_id or email_message_id,
                )
                if match and match.record.id != record_id:
                    duplicate_state, previous_reference, reimport = self._handle_duplicate(
                        match, record_id
                    )
            elif self.intake_history and record_id is not None:
                existing = self.intake_history.get(record_id)
                if existing.status == "REIMPORT_CONFIRMED":
                    reimport = True
                    duplicate_state = "confirmed"
                    if self._approved_duplicate_records:
                        previous = self.intake_history.get(
                            next(iter(self._approved_duplicate_records))
                        )
                        previous_reference = previous.safe_reference

            extracted = self.extractor.extract(archive)
            self.output("Extração.............. OK")
            self._history_update(record_id, "EXTRACTED")
            payload_files = self._source_files(extracted)
            payload_total_size = sum(path.stat().st_size for path in payload_files)
            confirmed_patient = self._confirm_patient(probable_name)
            self.output("Identificação......... OK")
            dicom_analysis = self.dicom_reader.analyze(
                extracted,
                confirmed_patient_name=confirmed_patient.name,
                confirmed_patient_id=confirmed_patient.patient_id,
            )
            self.dicom_reader.write_reports(dicom_analysis, extracted)
            self.output("Leitura DICOM......... OK")
            self._show_dicom_summary(dicom_analysis)
            if dicom_analysis.requires_manual_review:
                raise SupervisedImportError(
                    "O pacote contém múltiplos pacientes DICOM; publicação bloqueada "
                    "até revisão humana."
                )
            clinical_date = self._resolve_clinical_exam_date(
                dicom_analysis=dicom_analysis,
                archive_name=archive.name,
                sender_exam_date=sender_exam_date,
                sender_exam_time=sender_exam_time,
                email_received_at=email_received_at,
                import_started_at=import_started_at,
            )
            patient_folder, folder_mode, folder_reason = self._choose_patient_folder(
                confirmed_patient.name,
                allow_auto=confirmed_patient.selection_mode == "auto",
            )
            files = self._source_files(extracted)
            total_size = payload_total_size
            duplicate_count = self._possible_duplicate_count(payload_files, patient_folder)
            destination = self._next_destination(
                patient_folder, clinical_date.exam_date,
                self._acquisition_folder_label(acquisition_metadata),
            )
            if self.intake_history:
                match = self._check_duplicate(
                    archive_filename=archive.name,
                    archive_size=archive.stat().st_size,
                    patient_id=confirmed_patient.patient_id,
                    destination=destination,
                )
                if match and match.record.id != record_id:
                    state, reference, approved = self._handle_duplicate(match, record_id)
                    if duplicate_state != "confirmed":
                        duplicate_state = (
                            "confirmed" if state == "confirmed" else "possible"
                        )
                    previous_reference = reference
                    reimport = reimport or approved
                self._history_update(
                    record_id, "READY_FOR_CONFIRMATION",
                    patient_id_hash=fingerprint(confirmed_patient.patient_id),
                    destination_fingerprint=fingerprint(destination),
                    file_count=len(payload_files), total_size=total_size,
                )

            self._show_preview(
                archive=archive, extracted=extracted, patient=confirmed_patient,
                patient_folder=patient_folder, destination=destination,
                file_count=len(payload_files), total_size=total_size,
                duplicate_count=duplicate_count,
                folder_selection_mode=folder_mode,
                folder_selection_reason=folder_reason,
            )
            result = self._copy_and_manifest(
                archive=archive, extracted=extracted, destination=destination,
                patient=confirmed_patient, files=files, total_size=total_size,
                source=source, folder_selection_mode=folder_mode,
                folder_selection_reason=folder_reason,
                dicom_analysis=dicom_analysis,
                clinical_date=clinical_date,
                payload_file_count=len(payload_files),
                intake_record={
                    "correlation_id": self.audit_logger.correlation_id,
                    "archive_sha256": archive_sha256,
                    "exam_id": acquisition_exam_id or archive_sha256,
                    "acquisition": acquisition_metadata,
                    "duplicate_check": duplicate_state,
                    "reimport": reimport,
                    "previous_record_reference": previous_reference,
                },
            )
            self._history_update(
                record_id, "COMPLETED",
                manifest_path_fingerprint=fingerprint(result.manifest_path),
                file_checksums_fingerprint=fingerprint(
                    json.dumps(result.checksums, sort_keys=True)
                ),
            )
            self.output("Finalização........... OK")
            return result
        except SupervisedImportCancelled:
            self._safe_terminal_status(record_id, "CANCELLED")
            raise
        except IntakeHistoryError as exc:
            self._emit_history(AuditEventType.IMPORT_HISTORY_WRITE_FAILED, "FAILED")
            raise SupervisedImportError(
                "Falha no histórico local; nenhuma cópia deve prosseguir."
            ) from exc
        except Exception:
            self._safe_terminal_status(record_id, "FAILED")
            raise

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _utc_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _parse_date(value: date | str | None) -> date | None:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text = str(value or "").strip().replace("-", "")
        if not re.fullmatch(r"\d{8}", text):
            return None
        try:
            return datetime.strptime(text, "%Y%m%d").date()
        except ValueError:
            return None

    @staticmethod
    def _parse_time(value: time | str | None) -> time | None:
        if isinstance(value, time):
            return value.replace(tzinfo=None)
        digits = re.sub(r"[^0-9]", "", str(value or "").strip())
        if len(digits) < 4:
            return None
        digits = (digits + "000000")[:6]
        try:
            return datetime.strptime(digits, "%H%M%S").time()
        except ValueError:
            return None

    @classmethod
    def _date_time_from_archive_name(cls, archive_name: str) -> tuple[date | None, time | None]:
        matches = re.findall(
            r"(?:^|[_\-\s])(\d{8})(\d{6})?(?=(?:\.[A-Za-z0-9]+)*$)",
            Path(archive_name).name,
        )
        if len(matches) != 1:
            return None, None
        raw_date, raw_time = matches[0]
        return cls._parse_date(raw_date), cls._parse_time(raw_time) if raw_time else None

    def _resolve_clinical_exam_date(
        self,
        *,
        dicom_analysis: DicomPackageAnalysis,
        archive_name: str,
        sender_exam_date: date | str | None,
        sender_exam_time: time | str | None,
        email_received_at: datetime | None,
        import_started_at: datetime,
    ) -> ClinicalExamDate:
        dicom_dates = {
            parsed
            for study in dicom_analysis.studies
            if (parsed := self._parse_date(study.study_date)) is not None
        }
        dicom_times = {
            parsed
            for study in dicom_analysis.studies
            if (parsed := self._parse_time(study.study_time)) is not None
        }
        if len(dicom_dates) == 1:
            return ClinicalExamDate(
                next(iter(dicom_dates)),
                next(iter(dicom_times)) if len(dicom_times) == 1 else None,
                "DICOM_STUDY_DATE",
                self._utc_datetime(email_received_at) if email_received_at else None,
                import_started_at,
            )
        filename_date, filename_time = self._date_time_from_archive_name(archive_name)
        if filename_date:
            return ClinicalExamDate(
                filename_date, filename_time, "ARCHIVE_FILENAME",
                self._utc_datetime(email_received_at) if email_received_at else None,
                import_started_at,
            )
        trusted_date = self._parse_date(sender_exam_date)
        if trusted_date:
            return ClinicalExamDate(
                trusted_date, self._parse_time(sender_exam_time), "TRUSTED_SENDER_METADATA",
                self._utc_datetime(email_received_at) if email_received_at else None,
                import_started_at,
            )
        if email_received_at:
            received = self._utc_datetime(email_received_at)
            return ClinicalExamDate(
                received.date(), received.time().replace(tzinfo=None), "EMAIL_RECEIVED_AT",
                received, import_started_at,
            )
        return ClinicalExamDate(
            self.today_provider(), None, "IMPORT_DATE_FALLBACK", None, import_started_at
        )

    def check_message_duplicate(
        self, gmail_message_id: str, transfer_url: str | None = None
    ) -> None:
        """Bloqueia mensagem concluída antes de qualquer novo download."""
        if not self.intake_history:
            return
        match = self._check_duplicate(
            gmail_message_id=gmail_message_id, transfer_url=transfer_url
        )
        if match:
            self._handle_duplicate(match, None)

    def register_download(
        self, *, archive_path: str | Path, archive_sha256: str,
        gmail_message_id: str, transfer_url: str,
    ) -> int | None:
        if not self.intake_history:
            return None
        archive = Path(archive_path).resolve()
        record = self.intake_history.create_downloaded(
            correlation_id=self.audit_logger.correlation_id,
            archive_filename=archive.name,
            archive_size=archive.stat().st_size,
            archive_sha256=archive_sha256,
            gmail_message_id=gmail_message_id,
            transfer_url=transfer_url,
        )
        self._emit_history(AuditEventType.INTAKE_RECORD_CREATED, "DOWNLOADED")
        match = self._check_duplicate(
            archive_sha256=archive_sha256, gmail_message_id=gmail_message_id,
        )
        if match and match.record.id != record.id:
            self._handle_duplicate(match, record.id)
        return record.id

    def cancel_registered_download(self, record_id: int | None) -> None:
        self._history_update(record_id, "CANCELLED")

    def _check_duplicate(self, **signals: Any) -> DuplicateMatch | None:
        if not self.intake_history:
            return None
        self._emit_history(AuditEventType.DUPLICATE_CHECK_STARTED, "STARTED")
        match = self.intake_history.find_duplicate(**signals)
        if match is None:
            self._emit_history(AuditEventType.DUPLICATE_NOT_FOUND, "CLEAR")
        else:
            event = (
                AuditEventType.DUPLICATE_CONFIRMED
                if match.level == "confirmed"
                else AuditEventType.DUPLICATE_POSSIBLE
            )
            self._emit_history(event, "DETECTED", match.criterion)
        return match

    def _handle_duplicate(
        self, match: DuplicateMatch, record_id: int | None
    ) -> tuple[str, str, bool]:
        reference = match.record.safe_reference
        if match.record.id in self._approved_duplicate_records:
            if record_id:
                self._history_update(
                    record_id, "REIMPORT_CONFIRMED", reimport_confirmed=1,
                    previous_record_reference=reference,
                )
            return match.level, reference, True
        if record_id:
            self._history_update(record_id, "DUPLICATE_DETECTED")
        self.output("Este exame já foi importado anteriormente.")
        self.output(f"Data: {match.record.completed_at_utc or match.record.created_at_utc}")
        self.output(f"Status: {match.record.status}")
        self.output(f"Identificador: {reference}")
        patient = match.record.patient_id_hash
        self.output(f"Paciente: patient-{patient[:12]}" if patient else "Paciente: não disponível")
        destination = match.record.destination_fingerprint
        self.output(
            f"Destino: destination-{destination[:12]}"
            if destination else "Destino: não disponível"
        )
        self.output(f"Critério: {match.criterion}")
        if not self.allow_reimport:
            raise SupervisedImportError(
                "Duplicidade detectada; reimportação bloqueada por padrão."
            )
        self._emit_history(AuditEventType.REIMPORT_REQUESTED, "REQUESTED", match.criterion)
        if self.input("Digite REIMPORTAR para prosseguir ou ENTER para cancelar: ").strip() != "REIMPORTAR":
            raise SupervisedImportCancelled("Reimportação cancelada pelo operador.")
        self._approved_duplicate_records.add(match.record.id)
        if record_id:
            self._history_update(
                record_id, "REIMPORT_CONFIRMED", reimport_confirmed=1,
                previous_record_reference=reference,
            )
        self._emit_history(AuditEventType.REIMPORT_CONFIRMED, "CONFIRMED", match.criterion)
        return match.level, reference, True

    def _history_update(self, record_id: int | None, status: str, **fields: Any) -> None:
        if self.intake_history and record_id is not None:
            self.intake_history.update(record_id, status, **fields)

    def _safe_terminal_status(self, record_id: int | None, status: str) -> None:
        if not self.intake_history or record_id is None:
            return
        try:
            current = self.intake_history.get(record_id)
            if current.status != "DUPLICATE_DETECTED":
                self.intake_history.update(record_id, status)
        except IntakeHistoryError:
            self._emit_history(AuditEventType.IMPORT_HISTORY_WRITE_FAILED, "FAILED")

    def _emit_history(
        self, event_type: AuditEventType, status: str, reason: str | None = None
    ) -> None:
        emit_safely(
            self.audit_logger, event_type, status=status, reason_code=reason
        )

    def _acquire_archive(
        self,
        archive_path: str | Path | None,
        email_message_id: str | None,
    ) -> tuple[Path, str, str, datetime | None]:
        if archive_path:
            archive = Path(archive_path).expanduser().resolve()
            if not archive.is_file():
                raise SupervisedImportError("O arquivo informado não existe.")
            probable_name = (
                TransferNowConnector.extrair_nome_paciente_do_arquivo(
                    archive.name
                )
            )
            if not probable_name:
                raise SupervisedImportError(
                    "Não foi possível identificar o paciente pelo arquivo."
                )
            return archive, probable_name, "local", None

        if self.gmail_connector is None:
            raise SupervisedImportError("O conector readonly do Gmail não está disponível.")
        message = self.gmail_connector.get_message(str(email_message_id))
        content = "\n".join(
            body for body in (message.text_body, message.html_body) if body
        )
        transfer = TransferNowConnector.interpretar(content, message.subject)
        if not transfer.original_filename or not transfer.patient_name_candidate:
            raise SupervisedImportError(
                "A mensagem não informa um arquivo radiológico válido."
            )
        archive = self.downloader(
            transfer.download_url,
            transfer.original_filename,
            self.quarantine_root,
        ).resolve()
        if not archive.is_file():
            raise SupervisedImportError("O download não produziu um arquivo local.")
        return archive, transfer.patient_name_candidate, "gmail", message.received_at

    def _confirm_patient(self, probable_name: str) -> ConfirmedPatient:
        try:
            candidates = list(
                self.patient_repository.find_candidates(probable_name)
            )
        except PatientRepositoryUnavailableError:
            self._audit_selection(
                AuditEventType.PATIENT_MANUAL_SELECTION_REQUIRED,
                "MANUAL_REQUIRED",
                ResolutionReason.PATIENT_SOURCE_UNAVAILABLE,
                candidate_count=0,
                confidence_score=0.0,
            )
            raise SupervisedImportError(
                "A fonte de pacientes está indisponível; importação interrompida."
            ) from None

        resolved = PatientResolver(
            InMemoryPatientRepository(candidates), self.audit_logger
        ).resolve(ImagingExam(patient_name=probable_name))
        if self._is_patient_auto_selectable(resolved):
            selected = next(
                candidate for candidate in candidates if candidate.id == resolved.patient_id
            )
            self.output(f"Paciente selecionado automaticamente: {selected.nome}")
            self.output("Motivo: único candidato elegível; score acima do limiar.")
            self._audit_selection(
                AuditEventType.PATIENT_AUTO_SELECTED,
                "AUTO_SELECTED",
                resolved.resolution_reason,
                candidate_count=resolved.candidate_count,
                confidence_score=resolved.confidence_score,
            )
            return ConfirmedPatient(
                selected.nome,
                selected.id,
                "auto",
                resolved.resolution_reason.value,
                resolved.confidence_score,
                resolved.candidate_count,
            )

        self._audit_selection(
            AuditEventType.PATIENT_MANUAL_SELECTION_REQUIRED,
            "MANUAL_REQUIRED",
            resolved.resolution_reason,
            candidate_count=resolved.candidate_count,
            confidence_score=resolved.confidence_score,
        )

        if not candidates:
            self.output("Nenhum candidato retornado pela fonte de pacientes.")
            confirmation = self.input(
                f'Confirmar manualmente o nome provável "{probable_name}"? '
                "Digite CONFIRMAR: "
            )
            if confirmation != "CONFIRMAR":
                raise SupervisedImportCancelled(
                    "Importação cancelada na confirmação do paciente."
                )
            return ConfirmedPatient(
                probable_name,
                None,
                selection_reason=resolved.resolution_reason.value,
                candidate_count=resolved.candidate_count,
            )

        self.output("Candidatos de paciente:")
        for index, candidate in enumerate(candidates, start=1):
            self.output(f"{index}. {candidate.nome} (PatientId {candidate.id})")
        selected = self._numbered_choice(
            "Selecione o número do paciente confirmado: ",
            candidates,
        )
        return ConfirmedPatient(
            selected.nome,
            selected.id,
            selection_reason=resolved.resolution_reason.value,
            confidence_score=PatientResolver.similarity_score(probable_name, selected.nome),
            candidate_count=resolved.candidate_count,
        )

    def _is_patient_auto_selectable(self, resolved: ResolvedPatient) -> bool:
        safe_reasons = {
            ResolutionReason.EXACT_NAME,
            ResolutionReason.NORMALIZED_NAME,
            ResolutionReason.SINGLE_HIGH_SCORE,
        }
        return bool(
            not self.force_manual_selection
            and self.patient_source_mode != "offline"
            and resolved.matched
            and not resolved.requires_manual_review
            and resolved.patient_id is not None
            and resolved.candidate_count == 1
            and resolved.confidence_score >= self.auto_select_min_score
            and resolved.resolution_reason in safe_reasons
        )

    def _choose_patient_folder(
        self, patient_name: str, *, allow_auto: bool = False
    ) -> tuple[Path, str, str]:
        safe_name = "".join(
            character if character.isalnum() or character in " -_" else "-"
            for character in patient_name
        ).strip(" .")
        if not safe_name or not any(character.isalnum() for character in patient_name):
            raise SupervisedImportError("O nome confirmado do paciente é inválido.")
        selected = (self.staging_root / safe_name).resolve()
        if not selected.is_relative_to(self.staging_root):
            raise SupervisedImportError("O destino temporário calculado é inseguro.")

        self.output(f"Destino remoto selecionado automaticamente: {patient_name}")
        self.output("Motivo: destino derivado do paciente confirmado.")
        self._audit_selection(
            AuditEventType.ONEDRIVE_FOLDER_AUTO_SELECTED,
            "AUTO_SELECTED",
            "CONFIRMED_PATIENT_FOLDER",
            folder_candidate_count=1,
        )
        return selected, "auto", "CONFIRMED_PATIENT_FOLDER"

    def _audit_selection(
        self, event_type: AuditEventType, status: str, reason: Any, **metadata: Any
    ) -> None:
        emit_safely(
            self.audit_logger,
            event_type,
            status=status,
            reason_code=reason,
            metadata={**metadata, "override_manual": self.force_manual_selection},
        )

    def _numbered_choice(self, prompt: str, options: Sequence[Any]) -> Any:
        value = self.input(prompt).strip()
        try:
            index = int(value) - 1
        except ValueError:
            index = -1
        if index < 0 or index >= len(options):
            raise SupervisedImportCancelled("Seleção humana inválida ou cancelada.")
        return options[index]

    def _source_files(self, extracted: Path) -> list[Path]:
        files = sorted(
            (path for path in extracted.rglob("*") if path.is_file()),
            key=lambda path: path.relative_to(extracted).as_posix(),
        )
        if not files:
            raise SupervisedImportError("A pasta extraída não contém arquivos.")
        if any(path.relative_to(extracted).as_posix() == "manifest.json" for path in files):
            raise SupervisedImportError(
                "O arquivo compactado contém um manifest.json reservado."
            )
        return files

    def _next_destination(
        self, patient_folder: Path, exam_date: date | None = None,
        label: str = "Radiologia",
    ) -> Path:
        patient_folder = patient_folder.resolve()
        if not patient_folder.is_relative_to(self.staging_root):
            raise SupervisedImportError("O destino calculado está fora da área temporária.")
        base = (
            patient_folder
            / "Exames de imagem"
            / f"{(exam_date or self.today_provider()).isoformat()} - {label}"
        )
        destination = base
        if not destination.resolve().is_relative_to(self.staging_root):
            raise SupervisedImportError("O destino calculado está fora da raiz permitida.")
        return destination

    @staticmethod
    def _acquisition_folder_label(metadata: dict[str, Any] | None) -> str:
        if not isinstance(metadata, dict):
            return "Radiologia"
        classifications = [
            str(value).strip() for value in metadata.get("classifications") or []
            if str(value).strip()
        ]
        if int(metadata.get("asset_count") or 0) > 1 or len(classifications) > 1:
            return "Documentação Radiológica"
        if len(classifications) == 1 and re.fullmatch(
            r"[\wÀ-ÿ -]{1,60}", classifications[0]
        ):
            return classifications[0]
        return "Radiologia"

    def _possible_duplicate_count(
        self,
        source_files: list[Path],
        patient_folder: Path,
    ) -> int:
        image_root = patient_folder / "Exames de imagem"
        if not image_root.is_dir():
            return 0
        existing_by_signature = {
            (path.name.casefold(), path.stat().st_size)
            for path in image_root.rglob("*")
            if path.is_file()
        }
        return sum(
            (path.name.casefold(), path.stat().st_size)
            in existing_by_signature
            for path in source_files
        )

    def _show_preview(
        self,
        *,
        archive: Path,
        extracted: Path,
        patient: ConfirmedPatient,
        patient_folder: Path,
        destination: Path,
        file_count: int,
        total_size: int,
        duplicate_count: int,
        folder_selection_mode: str,
        folder_selection_reason: str,
    ) -> None:
        self.output(f"Arquivo de origem: {archive}")
        self.output(f"Pasta extraída: {extracted}")
        self.output(f"Paciente confirmado: {patient.name}")
        self.output(
            "PatientId: "
            f"{mask_patient_id(patient.patient_id) or 'não disponível'}"
        )
        self.output(f"Pasta do paciente: {patient_folder}")
        self.output(f"Área temporária local: {destination}")
        self.output(
            "Destino final no OneDrive: "
            f"{self._remote_destination(patient.name, destination.name)}"
        )
        self.output(f"Quantidade de arquivos: {file_count}")
        self.output(f"Tamanho total: {total_size} bytes")
        self.output(
            "Coincidências locais por nome e tamanho (apenas diagnóstico): "
            f"{duplicate_count}. Não autorizam nem bloqueiam o upload remoto."
        )
        self.output(f"Feature flag de auto-seleção ativa: {self.auto_select_unambiguous}")
        self.output(
            "Motivo da seleção: "
            f"paciente={patient.selection_reason}; pasta={folder_selection_reason}"
        )

    def _show_dicom_summary(self, analysis: DicomPackageAnalysis) -> None:
        self.output(f"DICOM válidos: {analysis.valid_dicom_count}")
        self.output(f"Estudos: {analysis.study_count}")
        self.output(f"Séries: {analysis.series_count}")
        self.output(f"Pacientes encontrados: {analysis.patient_count}")
        self.output(f"Classificação provável: {analysis.probable_classification}")
        self.output(
            "Alertas: " + ("; ".join(analysis.alerts) if analysis.alerts else "nenhum")
        )


    def _copy_and_manifest(
        self,
        *,
        archive: Path,
        extracted: Path,
        destination: Path,
        patient: ConfirmedPatient,
        files: list[Path],
        total_size: int,
        source: str,
        folder_selection_mode: str = "manual",
        folder_selection_reason: str = "FOLDER_MANUAL_SELECTION",
        intake_record: dict[str, Any] | None = None,
        dicom_analysis: DicomPackageAnalysis | None = None,
        clinical_date: ClinicalExamDate | None = None,
        payload_file_count: int | None = None,
    ) -> SupervisedImportResult:
        if not destination.resolve().is_relative_to(self.staging_root):
            raise SupervisedImportError(
                "O destino confirmado está fora da raiz permitida."
            )
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise SupervisedImportError(
                "Não foi possível criar o destino confirmado."
            ) from None
        checksums: dict[str, str] = {}
        for source_file in files:
            relative = source_file.relative_to(extracted)
            target = destination / relative
            if not target.resolve().is_relative_to(destination.resolve()):
                raise SupervisedImportError("Caminho de cópia fora do destino permitido.")
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            try:
                if target.is_file() and self._file_sha256(target) == self._file_sha256(
                    source_file
                ):
                    checksums[relative.as_posix()] = self._file_sha256(source_file)
                    continue
                with source_file.open("rb") as source_stream, target.open("xb") as target_stream:
                    while chunk := source_stream.read(1024 * 1024):
                        target_stream.write(chunk)
                        digest.update(chunk)
            except FileExistsError:
                raise SupervisedImportError(
                    "A cópia foi interrompida; nenhuma sobrescrita foi realizada."
                ) from None
            except OSError:
                raise SupervisedImportError(
                    "A cópia foi interrompida por uma falha local segura."
                ) from None
            checksums[relative.as_posix()] = digest.hexdigest()

        timestamp = self.now_provider()
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        timestamp = timestamp.astimezone(timezone.utc)
        source_archive_sha256 = (
            (intake_record or {}).get("archive_sha256")
            or self._file_sha256(archive)
        )
        exam_id = str((intake_record or {}).get("exam_id") or source_archive_sha256)
        clinical_date = clinical_date or ClinicalExamDate(
            self.today_provider(), None, "IMPORT_DATE_FALLBACK", None,
            self._utc_datetime(timestamp),
        )
        reported_file_count = payload_file_count if payload_file_count is not None else len(files)
        publication_total_bytes = sum(path.stat().st_size for path in files)
        report_names = {"dicom_summary.json", "resumo_do_exame.txt"}
        reported_checksums = {
            name: digest
            for name, digest in checksums.items()
            if name not in report_names
        }
        manifest = {
            "correlation_id": self.audit_logger.correlation_id,
            "timestamp_utc": timestamp.isoformat().replace("+00:00", "Z"),
            "exam_date": clinical_date.exam_date.isoformat(),
            "exam_time": clinical_date.exam_time.isoformat() if clinical_date.exam_time else None,
            "exam_date_source": clinical_date.exam_date_source,
            "email_received_at": (
                clinical_date.email_received_at.isoformat().replace("+00:00", "Z")
                if clinical_date.email_received_at else None
            ),
            "import_started_at": clinical_date.import_started_at.isoformat().replace("+00:00", "Z"),
            "import_completed_at": None,
            "original_archive": archive.name,
            "masked_patient_id": mask_patient_id(patient.patient_id),
            "file_count": reported_file_count,
            "total_size_bytes": total_size,
            "checksums": reported_checksums,
            "source": source,
            "destination": str(destination),
            "onedrive_destination": self._remote_destination(
                patient.name, destination.name
            ),
            "status": "IN_PROGRESS",
            "publication": {
                "state": "IN_PROGRESS",
                "exam_id": exam_id,
                "source_archive_sha256": source_archive_sha256,
                "total_files": len(files),
                "total_bytes": publication_total_bytes,
                "uploaded_files_count": 0,
                "uploaded_bytes": 0,
                "uploaded_files": {},
            },
            "selection_mode": {
                "patient": patient.selection_mode,
                "folder": folder_selection_mode,
            },
            "auto_selection_reason": {
                "patient": patient.selection_reason,
                "folder": folder_selection_reason,
            },
            "intake_record": intake_record or {
                "correlation_id": self.audit_logger.correlation_id,
                "archive_sha256": self._file_sha256(archive),
                "duplicate_check": "clear",
                "reimport": False,
                "previous_record_reference": None,
            },
            "acquisition": (intake_record or {}).get("acquisition"),
            "dicom_intelligence": (
                dicom_analysis.to_dict() if dicom_analysis is not None else None
            ),
        }
        manifest_path = destination / "manifest.json"
        try:
            with manifest_path.open("w", encoding="utf-8") as output:
                json.dump(
                    manifest,
                    output,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                output.write("\n")
        except OSError:
            raise SupervisedImportError(
                "Os arquivos foram copiados, mas o manifesto não pôde ser criado."
            ) from None
        onedrive_destination = self._publish_to_onedrive(
            patient_name=patient.name,
            local_destination=destination,
            manifest_path=manifest_path,
            checksums=checksums,
        )
        if self.exam_index_service is not None:
            try:
                self.exam_index_service.index_manifest(
                    self._read_manifest(manifest_path),
                    patient_name=patient.name,
                    source=str(
                        ((intake_record or {}).get("acquisition") or {}).get(
                            "provider_id"
                        ) or "pipeline"
                    ),
                )
                self.output("Indexação............. OK")
            except ExamIndexError:
                LOGGER.exception(
                    "Falha no índice derivado; exame remoto preservado para rebuild."
                )
                self.output("Indexação............. PENDENTE (rebuild poderá recuperar)")
        return SupervisedImportResult(
            destination=destination,
            manifest_path=manifest_path,
            onedrive_destination=onedrive_destination,
            file_count=reported_file_count,
            total_size_bytes=total_size,
            checksums=reported_checksums,
        )

    def _remote_destination(self, patient_name: str, folder_name: str) -> str:
        return "/".join(
            (self.onedrive_root, patient_name, "Radiologia", folder_name)
        )

    def _publish_to_onedrive(
        self,
        *,
        patient_name: str,
        local_destination: Path,
        manifest_path: Path,
        checksums: dict[str, str],
    ) -> str:
        """Publica ou retoma uma importação identificada pelo manifesto remoto."""
        remote_destination: GraphFolder | None = None
        manifest = self._read_manifest(manifest_path)
        publication = manifest["publication"]
        source_sha256 = str(publication.get("source_archive_sha256") or "")
        exam_id = str(publication.get("exam_id") or source_sha256)
        started_at = self.monotonic_provider()
        try:
            root = self.onedrive_client.find_root_folder(self.onedrive_root)
            patient = self._find_or_create_normalized_folder(root, patient_name)
            radiology = self._find_or_create_normalized_folder(patient, "Radiologia")
            same_exam = self._folders_for_exam_id(radiology, exam_id)
            if len(same_exam) > 1:
                raise SupervisedImportError(
                    self._destination_diagnostic(same_exam, exam_id)
                )
            candidates = same_exam or self._destination_candidates(
                radiology, local_destination.name
            )
            if len(candidates) > 1:
                raise SupervisedImportError(
                    self._destination_diagnostic(candidates, exam_id)
                )
            if candidates:
                remote_destination = candidates[0]
                remote_manifest = self.onedrive_client.download_json_file(
                    remote_destination, "manifest.json"
                )
                remote_publication = (
                    remote_manifest.get("publication")
                    if isinstance(remote_manifest, dict)
                    else None
                )
                if not isinstance(remote_publication, dict):
                    raise SupervisedImportError(
                        "Destino remoto existente sem manifesto de estado confiável; "
                        "nenhuma alteração foi feita e a consolidação exige confirmação explícita."
                    )
                remote_exam_id = str(
                    remote_publication.get("exam_id")
                    or remote_publication.get("source_archive_sha256") or ""
                )
                if remote_exam_id != exam_id:
                    remote_destination = None
                    for alternate_name in self._disambiguated_destination_names(manifest):
                        alternate = self.onedrive_client.find_child_folder(
                            radiology, alternate_name
                        )
                        if alternate is None:
                            remote_destination = self.onedrive_client.create_folder(
                                radiology, alternate_name
                            )
                            remote_publication = publication
                            break
                        alternate_manifest = self.onedrive_client.download_json_file(
                            alternate, "manifest.json"
                        )
                        alternate_publication = (
                            alternate_manifest.get("publication")
                            if isinstance(alternate_manifest, dict) else None
                        )
                        if isinstance(alternate_publication, dict) and str(
                            alternate_publication.get("exam_id")
                            or alternate_publication.get("source_archive_sha256") or ""
                        ) == exam_id:
                            remote_destination = alternate
                            remote_publication = alternate_publication
                            break
                    if remote_destination is None:
                        raise SupervisedImportError(
                            "Não foi possível resolver um destino clínico estável; "
                            "nenhuma sobrescrita foi realizada."
                        )
                state = str(remote_publication.get("state") or "").upper()
                if state == "COMPLETE":
                    remote_uploaded = remote_publication.get("uploaded_files")
                    if not isinstance(remote_uploaded, dict):
                        raise SupervisedImportError(
                            "A importação remota COMPLETE não possui inventário "
                            "verificável; novo upload bloqueado."
                        )
                    expected_paths = set(checksums)
                    if set(remote_uploaded) != expected_paths:
                        raise SupervisedImportError(
                            "A importação remota COMPLETE diverge do pacote local; "
                            "novo upload bloqueado."
                        )
                    verified = all(
                        isinstance(remote_uploaded.get(relative_name), dict)
                        and remote_uploaded[relative_name].get("sha256")
                        == checksum
                        and isinstance(
                            remote_uploaded[relative_name].get("size"), int
                        )
                        and self._remote_file_matches(
                            remote_destination,
                            Path(relative_name),
                            remote_uploaded[relative_name]["size"],
                        )
                        for relative_name, checksum in checksums.items()
                    )
                    if not verified:
                        raise SupervisedImportError(
                            "A importação remota COMPLETE não pôde ser validada "
                            "integralmente; novo upload bloqueado."
                        )
                    remote_path = "/".join(
                        (
                            self.onedrive_root,
                            patient.name,
                            radiology.name,
                            remote_destination.name,
                        )
                    )
                    publication.clear()
                    publication.update(
                        json.loads(json.dumps(remote_publication))
                    )
                    manifest["onedrive_destination"] = remote_path
                    manifest["status"] = "COMPLETED"
                    manifest["import_completed_at"] = (
                        remote_manifest.get("import_completed_at")
                        or manifest.get("import_completed_at")
                        or self._utc_datetime(self.now_provider())
                        .isoformat()
                        .replace("+00:00", "Z")
                    )
                    self._write_manifest(manifest_path, manifest)
                    self.output(
                        "Publicação remota COMPLETE validada e reutilizada; "
                        "nenhum upload adicional foi necessário."
                    )
                    return remote_path
                publication["uploaded_files"] = dict(
                    remote_publication.get("uploaded_files") or {}
                )
            else:
                try:
                    remote_destination = self.onedrive_client.create_folder(
                        radiology, local_destination.name
                    )
                except OneDriveFolderConflictError:
                    raise SupervisedImportError(
                        "O destino remoto passou a existir; nova tentativa deve diagnosticá-lo "
                        "antes de continuar."
                    ) from None

            remote_path = "/".join(
                (
                    self.onedrive_root,
                    patient.name,
                    radiology.name,
                    remote_destination.name,
                )
            )
            manifest["onedrive_destination"] = remote_path

            uploaded_files = publication["uploaded_files"]
            verified_files: dict[str, dict[str, Any]] = {}
            uploaded_bytes = 0
            for relative_name, record in uploaded_files.items():
                if not isinstance(record, dict):
                    continue
                expected_sha = checksums.get(relative_name)
                expected_size = record.get("size")
                local_file = local_destination / Path(relative_name)
                if (
                    expected_sha
                    and record.get("sha256") == expected_sha
                    and isinstance(expected_size, int)
                    and local_file.is_file()
                    and local_file.stat().st_size == expected_size
                    and self._remote_file_matches(
                        remote_destination, Path(relative_name), expected_size
                    )
                ):
                    verified_files[relative_name] = record
                    uploaded_bytes += expected_size
            publication.update(
                state="IN_PROGRESS",
                uploaded_files=verified_files,
                uploaded_files_count=len(verified_files),
                uploaded_bytes=uploaded_bytes,
            )
            self._write_manifest(manifest_path, manifest)
            self.onedrive_client.upload_small_file(
                remote_destination, manifest_path, remote_filename="manifest.json"
            )

            folders: dict[Path, GraphFolder] = {Path(): remote_destination}
            completed_files = len(verified_files)
            session_start_bytes = uploaded_bytes
            for relative_name, checksum in checksums.items():
                if relative_name in verified_files:
                    continue
                relative = Path(relative_name)
                local_file = local_destination / relative
                remote_parent = self._ensure_remote_parent(
                    remote_destination, relative.parent, folders
                )
                prior_bytes = uploaded_bytes

                def on_progress(current: int, file_size: int) -> None:
                    self._show_upload_progress(
                        completed_files=(
                            completed_files + 1 if current >= file_size else completed_files
                        ),
                        total_files=int(publication["total_files"]),
                        uploaded_bytes=prior_bytes + current,
                        total_bytes=int(publication["total_bytes"]),
                        current_file=relative_name,
                        started_at=started_at,
                        session_start_bytes=session_start_bytes,
                    )

                def on_retry(offset: int, attempt: int, maximum: int) -> None:
                    self.progress_output(
                        "Falha transitória; retomando bloco a partir do byte "
                        f"{offset}. Tentativa {attempt}/{maximum}."
                    )

                self.onedrive_client.upload_small_file(
                    remote_parent,
                    local_file,
                    remote_filename=relative.name,
                    progress_callback=on_progress,
                    retry_callback=on_retry,
                )
                file_size = local_file.stat().st_size
                uploaded_bytes += file_size
                completed_files += 1
                verified_files[relative_name] = {
                    "sha256": checksum,
                    "size": file_size,
                }
                publication.update(
                    uploaded_files=verified_files,
                    uploaded_files_count=completed_files,
                    uploaded_bytes=uploaded_bytes,
                )
                self._write_manifest(manifest_path, manifest)
                self.onedrive_client.upload_small_file(
                    remote_destination, manifest_path, remote_filename="manifest.json"
                )

            publication["state"] = "COMPLETE"
            manifest["status"] = "COMPLETED"
            manifest["import_completed_at"] = self._utc_datetime(
                self.now_provider()
            ).isoformat().replace("+00:00", "Z")
            self._write_manifest(manifest_path, manifest)
            self.onedrive_client.upload_small_file(
                remote_destination, manifest_path, remote_filename="manifest.json"
            )
            self.output(
                f"Upload concluído: {completed_files}/{publication['total_files']} arquivos, "
                f"{self._format_bytes(uploaded_bytes)} enviados."
            )
            return remote_path
        except SupervisedImportError as exc:
            self.output(f"Upload não concluído: {exc}")
            raise
        except OneDriveGraphError as exc:
            if remote_destination is not None:
                publication["state"] = "FAILED"
                manifest["status"] = "FAILED"
                self._write_manifest(manifest_path, manifest)
                try:
                    self.onedrive_client.upload_small_file(
                        remote_destination, manifest_path, remote_filename="manifest.json"
                    )
                except OneDriveGraphError:
                    LOGGER.exception("Falha adicional ao registrar estado FAILED no OneDrive.")
            self.output("Upload falhou; o estado foi preservado para retomada.")
            LOGGER.exception("Falha ao publicar exame no OneDrive: %s", exc)
            raise SupervisedImportError(str(exc)) from exc

    def _find_or_create_normalized_folder(
        self, parent: GraphFolder, requested_name: str
    ) -> GraphFolder:
        normalized = PatientNormalizer.compare_ready(requested_name)
        matches = []
        for item in self.onedrive_client.list_children(parent):
            if not self._is_folder_item(item):
                continue
            name = str(item.get("name") or "")
            if PatientNormalizer.compare_ready(name) == normalized:
                folder = self.onedrive_client.find_child_folder(parent, name)
                if folder is not None:
                    matches.append(folder)
        unique = {folder.item_id: folder for folder in matches}
        if len(unique) > 1:
            names = ", ".join(folder.name for folder in unique.values())
            raise SupervisedImportError(
                "Mais de uma pasta remota corresponde ao mesmo nome normalizado: "
                f"{names}. Nenhuma alteração foi realizada."
            )
        if unique:
            return next(iter(unique.values()))
        return self.onedrive_client.create_folder(parent, requested_name)

    def _destination_candidates(
        self, radiology: GraphFolder, base_name: str
    ) -> list[GraphFolder]:
        normalized_base = PatientNormalizer.compare_ready(base_name)
        matches = []
        for item in self.onedrive_client.list_children(radiology):
            if not self._is_folder_item(item):
                continue
            name = str(item.get("name") or "")
            normalized = PatientNormalizer.compare_ready(name)
            if normalized == normalized_base or re.fullmatch(
                rf"{re.escape(normalized_base)} \d+", normalized
            ):
                folder = self.onedrive_client.find_child_folder(radiology, name)
                if folder is not None:
                    matches.append(folder)
        return list({folder.item_id: folder for folder in matches}.values())

    def _folders_for_exam_id(
        self, radiology: GraphFolder, exam_id: str
    ) -> list[GraphFolder]:
        matches: list[GraphFolder] = []
        if not exam_id:
            return matches
        for item in self.onedrive_client.list_children(radiology):
            if not self._is_folder_item(item):
                continue
            folder = self.onedrive_client.find_child_folder(
                radiology, str(item.get("name") or "")
            )
            if folder is None:
                continue
            remote_manifest = self.onedrive_client.download_json_file(folder, "manifest.json")
            publication = (
                remote_manifest.get("publication")
                if isinstance(remote_manifest, dict) else None
            )
            if isinstance(publication, dict) and str(
                publication.get("exam_id")
                or publication.get("source_archive_sha256") or ""
            ) == exam_id:
                matches.append(folder)
        return list({folder.item_id: folder for folder in matches}.values())

    @staticmethod
    def _disambiguated_destination_names(manifest: dict[str, Any]) -> list[str]:
        exam_date = str(manifest.get("exam_date") or "data-desconhecida")
        exam_time = str(manifest.get("exam_time") or "")
        names: list[str] = []
        if re.fullmatch(r"\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?", exam_time):
            names.append(f"{exam_date} {exam_time[:5].replace(':', '-')} - Radiologia")
        studies = (manifest.get("dicom_intelligence") or {}).get("studies") or []
        modalities = sorted({
            str(study.get("modality") or "").strip().upper()
            for study in studies if isinstance(study, dict) and study.get("modality")
        })
        if len(modalities) == 1 and re.fullmatch(r"[A-Z0-9]{1,16}", modalities[0]):
            names.append(f"{exam_date} - Radiologia - {modalities[0]}")
        exam_id = str((manifest.get("publication") or {}).get("exam_id") or "")
        names.append(f"{exam_date} - Radiologia - {exam_id[:12]}")
        return list(dict.fromkeys(names))

    def _destination_diagnostic(
        self, candidates: list[GraphFolder], exam_id: str
    ) -> str:
        states = []
        for folder in candidates:
            manifest = self.onedrive_client.download_json_file(folder, "manifest.json")
            publication = (
                manifest.get("publication") if isinstance(manifest, dict) else None
            )
            state = (
                str(publication.get("state") or "UNKNOWN")
                if isinstance(publication, dict)
                else "UNKNOWN"
            )
            same_exam = bool(
                isinstance(publication, dict)
                and str(
                    publication.get("exam_id")
                    or publication.get("source_archive_sha256") or ""
                ) == exam_id
            )
            states.append(
                f"{folder.name}: estado={state}, mesmo_exame={'sim' if same_exam else 'não'}"
            )
        return (
            "Foram encontradas múltiplas pastas de destino. Diagnóstico: "
            + "; ".join(states)
            + ". Nenhuma pasta foi movida, mesclada ou apagada; consolidação exige "
            "confirmação explícita."
        )

    def _remote_file_matches(
        self, destination: GraphFolder, relative: Path, expected_size: int
    ) -> bool:
        parent = destination
        for part in relative.parent.parts:
            child = self.onedrive_client.find_child_folder(parent, part)
            if child is None:
                return False
            parent = child
        return any(
            str(item.get("name") or "") == relative.name
            and isinstance(item.get("file"), dict)
            and item.get("size") == expected_size
            for item in self.onedrive_client.list_children(parent)
        )

    def _ensure_remote_parent(
        self,
        destination: GraphFolder,
        relative_parent: Path,
        folders: dict[Path, GraphFolder],
    ) -> GraphFolder:
        remote_parent = destination
        current = Path()
        for part in relative_parent.parts:
            current /= part
            existing = folders.get(current)
            if existing is None:
                existing = self._find_or_create_normalized_folder(remote_parent, part)
                folders[current] = existing
            remote_parent = existing
        return remote_parent

    def _show_upload_progress(
        self,
        *,
        completed_files: int,
        total_files: int,
        uploaded_bytes: int,
        total_bytes: int,
        current_file: str,
        started_at: float,
        session_start_bytes: int,
    ) -> None:
        elapsed = max(self.monotonic_provider() - started_at, 0.001)
        speed = max(uploaded_bytes - session_start_bytes, 0) / elapsed
        remaining = max(total_bytes - uploaded_bytes, 0)
        eta = remaining / speed if speed > 0 else 0
        progress = (uploaded_bytes / total_bytes * 100) if total_bytes else 100.0
        self.progress_output(
            f"Upload: {completed_files}/{total_files} arquivos | "
            f"Dados: {self._format_bytes(uploaded_bytes)} / {self._format_bytes(total_bytes)} | "
            f"Progresso: {progress:.1f}% | Arquivo atual: {current_file} | "
            f"Velocidade média: {self._format_bytes(int(speed))}/s | "
            f"Tempo decorrido: {self._format_duration(elapsed)} | "
            f"ETA: {self._format_duration(eta)}"
        )

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = float(value)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if amount < 1024 or unit == "TiB":
                return f"{amount:.2f} {unit}" if unit != "B" else f"{int(amount)} B"
            amount /= 1024
        return f"{amount:.2f} TiB"

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total = max(int(seconds), 0)
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    @staticmethod
    def _is_folder_item(item: dict[str, Any]) -> bool:
        return isinstance(item.get("folder"), dict) or (
            isinstance(item.get("remoteItem"), dict)
            and isinstance(item["remoteItem"].get("folder"), dict)
        )

    @staticmethod
    def _read_manifest(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            raise SupervisedImportError("Manifesto local de publicação inválido.") from None
        if not isinstance(value, dict) or not isinstance(value.get("publication"), dict):
            raise SupervisedImportError("Manifesto local de publicação inválido.")
        return value

    @staticmethod
    def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
        try:
            path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError:
            raise SupervisedImportError(
                "Não foi possível atualizar o manifesto de publicação."
            ) from None

    def _download_transfernow(
        self,
        url: str,
        filename: str,
        quarantine_root: Path,
    ) -> Path:
        try:
            parts = urlsplit(url)
            hostname = (parts.hostname or "").casefold().rstrip(".")
            scheme = parts.scheme.casefold()
        except ValueError:
            hostname = ""
            scheme = ""
        if (
            scheme != "https"
            or not (
                hostname == "transfernow.net"
                or hostname.endswith(".transfernow.net")
            )
        ):
            raise SupervisedImportError("O endereço de download não é permitido.")
        safe_name = Path(filename).name
        if (
            safe_name != filename
            or Path(safe_name).suffix.casefold() not in {".zip", ".rar"}
        ):
            raise SupervisedImportError("Nome de arquivo inseguro no download.")
        quarantine_root.mkdir(parents=True, exist_ok=True)
        destination = self._next_download_path(quarantine_root, safe_name)
        try:
            response = requests.get(url, stream=True, timeout=60)
            response.raise_for_status()
            with destination.open("xb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        output.write(chunk)
        except (OSError, requests.RequestException):
            raise SupervisedImportError(
                "Não foi possível baixar o arquivo do TransferNow. Use --archive-path."
            ) from None
        return destination

    @staticmethod
    def _next_download_path(root: Path, filename: str) -> Path:
        original = Path(filename)
        candidate = root / original.name
        suffix = 2
        while candidate.exists():
            candidate = root / f"{original.stem} ({suffix}){original.suffix}"
            suffix += 1
        if not candidate.resolve().is_relative_to(root.resolve()):
            raise SupervisedImportError("Destino de download fora da quarentena.")
        return candidate
