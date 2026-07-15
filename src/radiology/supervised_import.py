"""MVP interativo de importação radiológica estritamente supervisionada."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any, Optional
from urllib.parse import urlsplit

import requests

from integrations.gmail_connector import GmailConnector
from integrations.onedrive_connector import PatientFolderLocator
from integrations.transfernow_connector import TransferNowConnector
from models.imaging_exam import ImagingExam
from models.patient import Patient
from models.resolved_patient import ResolvedPatient, ResolutionReason
from observability.audit_logger import AuditEventType, AuditLogger, emit_safely, mask_patient_id
from radiology.archive_extractor import ArchiveExtractor
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
class SupervisedImportResult:
    destination: Path
    manifest_path: Path
    file_count: int
    total_size_bytes: int
    checksums: dict[str, str]


class SupervisedRadiologyImporter:
    """Executa aquisição, revisão e cópia somente após confirmação humana."""

    def __init__(
        self,
        *,
        patients_root: str | Path,
        quarantine_root: str | Path,
        archive_tool_path: str | Path,
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
    ) -> None:
        self.patients_root = Path(patients_root).expanduser().resolve()
        self.quarantine_root = Path(quarantine_root).expanduser().resolve()
        self.patient_repository = patient_repository
        self.gmail_connector = gmail_connector
        self.audit_logger = audit_logger or AuditLogger()
        self.input = input_func
        self.output = output
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
        self.folder_locator = PatientFolderLocator(self.patients_root)

    def run(
        self,
        *,
        archive_path: str | Path | None = None,
        email_message_id: str | None = None,
        gmail_message_id: str | None = None,
        transfer_url: str | None = None,
        archive_sha256: str | None = None,
        intake_record_id: int | None = None,
    ) -> SupervisedImportResult:
        if bool(archive_path) == bool(email_message_id):
            raise SupervisedImportError(
                "Informe exatamente uma entrada: e-mail ou arquivo local."
            )

        record_id: int | None = intake_record_id
        duplicate_state = "clear"
        previous_reference: str | None = None
        reimport = False
        try:
            archive, probable_name, source = self._acquire_archive(
                archive_path, email_message_id,
            )
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
            self._history_update(record_id, "EXTRACTED")
            confirmed_patient = self._confirm_patient(probable_name)
            patient_folder, folder_mode, folder_reason = self._choose_patient_folder(
                confirmed_patient.name,
                allow_auto=confirmed_patient.selection_mode == "auto",
            )
            files = self._source_files(extracted)
            total_size = sum(path.stat().st_size for path in files)
            duplicate_count = self._possible_duplicate_count(files, patient_folder)
            destination = self._next_destination(patient_folder)
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
                    file_count=len(files), total_size=total_size,
                )

            self._show_preview(
                archive=archive, extracted=extracted, patient=confirmed_patient,
                patient_folder=patient_folder, destination=destination,
                file_count=len(files), total_size=total_size,
                duplicate_count=duplicate_count,
                folder_selection_mode=folder_mode,
                folder_selection_reason=folder_reason,
            )
            confirmation = self.input(
                "Digite CONFIRMAR (não diferencia maiúsculas/minúsculas)\n"
                "ou pressione ENTER para cancelar.\n"
                "[ENTER] = cancelar\nCONFIRMAR = copiar\n> "
            )
            if confirmation.strip().casefold() != "confirmar":
                raise SupervisedImportCancelled(
                    "Importação cancelada: confirmação explícita não recebida."
                )
            result = self._copy_and_manifest(
                archive=archive, extracted=extracted, destination=destination,
                patient=confirmed_patient, files=files, total_size=total_size,
                source=source, folder_selection_mode=folder_mode,
                folder_selection_reason=folder_reason,
                intake_record={
                    "correlation_id": self.audit_logger.correlation_id,
                    "archive_sha256": archive_sha256,
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
    ) -> tuple[Path, str, str]:
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
            return archive, probable_name, "local"

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
        return archive, transfer.patient_name_candidate, "gmail"

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
            self.auto_select_unambiguous
            and not self.force_manual_selection
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
        matches = self.folder_locator.find_compatible(patient_name)
        if not matches:
            raise SupervisedImportError(
                "Nenhuma pasta compatível de paciente foi encontrada."
            )

        can_auto_select = bool(
            self.auto_select_unambiguous
            and allow_auto
            and not self.force_manual_selection
            and self.patient_source_mode != "offline"
            and len(matches) == 1
            and self._similar_folder_count(patient_name) == 1
            and "REVIEW REQUIRED" not in matches[0].name.upper().replace("_", " ")
        )
        if can_auto_select:
            selected = self.folder_locator.validate_selection(matches[0])
            self.output(f"Pasta selecionada automaticamente: {selected}")
            self.output("Motivo: única pasta compatível e coerente.")
            self._audit_selection(
                AuditEventType.ONEDRIVE_FOLDER_AUTO_SELECTED,
                "AUTO_SELECTED",
                "SINGLE_COMPATIBLE_FOLDER",
                folder_candidate_count=1,
            )
            return selected, "auto", "SINGLE_COMPATIBLE_FOLDER"

        self._audit_selection(
            AuditEventType.ONEDRIVE_FOLDER_MANUAL_SELECTION_REQUIRED,
            "MANUAL_REQUIRED",
            "FOLDER_MANUAL_SELECTION",
            folder_candidate_count=len(matches),
        )
        self.output("Pastas compatíveis no OneDrive local:")
        for index, folder in enumerate(matches, start=1):
            self.output(f"{index}. {folder}")
        selected = self._numbered_choice(
            "Selecione o número da pasta confirmada: ",
            matches,
        )
        return self.folder_locator.validate_selection(selected), "manual", "FOLDER_MANUAL_SELECTION"

    def _similar_folder_count(self, patient_name: str) -> int:
        if not self.patients_root.is_dir():
            return 0
        return sum(
            1
            for folder in self.patients_root.iterdir()
            if folder.is_dir()
            and folder.resolve().is_relative_to(self.patients_root)
            and PatientResolver.similarity_score(patient_name, folder.name)
            >= PatientResolver.MINIMUM_MATCH_SCORE
        )

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

    def _next_destination(self, patient_folder: Path) -> Path:
        patient_folder = self.folder_locator.validate_selection(patient_folder)
        base = (
            patient_folder
            / "Exames de imagem"
            / f"{self.today_provider().isoformat()} - Radiologia"
        )
        destination = base
        suffix = 2
        while destination.exists():
            destination = base.with_name(f"{base.name} ({suffix})")
            suffix += 1
        if not destination.resolve().is_relative_to(self.patients_root):
            raise SupervisedImportError("O destino calculado está fora da raiz permitida.")
        return destination

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
        self.output(f"Destino final: {destination}")
        self.output(f"Quantidade de arquivos: {file_count}")
        self.output(f"Tamanho total: {total_size} bytes")
        self.output(f"Possíveis duplicados: {duplicate_count}")
        self.output(f"Feature flag de auto-seleção ativa: {self.auto_select_unambiguous}")
        self.output(
            "Motivo da seleção: "
            f"paciente={patient.selection_reason}; pasta={folder_selection_reason}"
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
    ) -> SupervisedImportResult:
        if not destination.resolve().is_relative_to(self.patients_root):
            raise SupervisedImportError(
                "O destino confirmado está fora da raiz permitida."
            )
        try:
            destination.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            raise SupervisedImportError(
                "O destino passou a existir; nenhuma sobrescrita foi realizada."
            ) from None
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
                with source_file.open("rb") as source_stream, target.open("xb") as target_stream:
                    while chunk := source_stream.read(1024 * 1024):
                        target_stream.write(chunk)
                        digest.update(chunk)
            except FileExistsError:
                raise SupervisedImportError(
                    "A cópia foi interrompida para evitar sobrescrita."
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
        manifest = {
            "correlation_id": self.audit_logger.correlation_id,
            "timestamp_utc": timestamp.isoformat().replace("+00:00", "Z"),
            "original_archive": archive.name,
            "masked_patient_id": mask_patient_id(patient.patient_id),
            "file_count": len(files),
            "total_size_bytes": total_size,
            "checksums": checksums,
            "source": source,
            "destination": str(destination),
            "status": "COMPLETED",
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
        }
        manifest_path = destination / "manifest.json"
        try:
            with manifest_path.open("x", encoding="utf-8") as output:
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
        return SupervisedImportResult(
            destination=destination,
            manifest_path=manifest_path,
            file_count=len(files),
            total_size_bytes=total_size,
            checksums=checksums,
        )

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
