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
from models.patient import Patient
from observability.audit_logger import AuditLogger, mask_patient_id
from radiology.archive_extractor import ArchiveExtractor
from repositories.patient_repository import (
    PatientRepository,
    PatientRepositoryUnavailableError,
)


class SupervisedImportError(RuntimeError):
    """Erro seguro que impede a importação supervisionada."""


class SupervisedImportCancelled(SupervisedImportError):
    """O operador não forneceu a confirmação explícita exigida."""


@dataclass(frozen=True)
class ConfirmedPatient:
    name: str
    patient_id: Optional[int]


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
        patient_repository: PatientRepository,
        gmail_connector: Optional[GmailConnector] = None,
        audit_logger: Optional[AuditLogger] = None,
        input_func: Callable[[str], str] = input,
        output: Callable[[str], None] = print,
        downloader: Optional[Callable[[str, str, Path], Path]] = None,
        today_provider: Callable[[], date] = date.today,
        now_provider: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
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
        self.extractor = ArchiveExtractor(
            self.quarantine_root,
            archive_tool_path,
        )
        self.folder_locator = PatientFolderLocator(self.patients_root)

    def run(
        self,
        *,
        archive_path: str | Path | None = None,
        email_message_id: str | None = None,
    ) -> SupervisedImportResult:
        if bool(archive_path) == bool(email_message_id):
            raise SupervisedImportError(
                "Informe exatamente uma entrada: e-mail ou arquivo local."
            )

        archive, probable_name, source = self._acquire_archive(
            archive_path,
            email_message_id,
        )
        extracted = self.extractor.extract(archive)
        confirmed_patient = self._confirm_patient(probable_name)
        patient_folder = self._choose_patient_folder(confirmed_patient.name)
        files = self._source_files(extracted)
        total_size = sum(path.stat().st_size for path in files)
        duplicate_count = self._possible_duplicate_count(
            files,
            patient_folder,
        )
        destination = self._next_destination(patient_folder)

        self._show_preview(
            archive=archive,
            extracted=extracted,
            patient=confirmed_patient,
            patient_folder=patient_folder,
            destination=destination,
            file_count=len(files),
            total_size=total_size,
            duplicate_count=duplicate_count,
        )
        confirmation = self.input(
            "Digite exatamente CONFIRMAR para copiar os arquivos: "
        )
        if confirmation != "CONFIRMAR":
            raise SupervisedImportCancelled(
                "Importação cancelada: confirmação explícita não recebida."
            )

        return self._copy_and_manifest(
            archive=archive,
            extracted=extracted,
            destination=destination,
            patient=confirmed_patient,
            files=files,
            total_size=total_size,
            source=source,
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
        if not transfer.filename or not transfer.patient_name_candidate:
            raise SupervisedImportError(
                "A mensagem não informa um arquivo radiológico válido."
            )
        archive = self.downloader(
            transfer.download_url,
            transfer.filename,
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
            raise SupervisedImportError(
                "A fonte de pacientes está indisponível; importação interrompida."
            ) from None

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
            return ConfirmedPatient(probable_name, None)

        self.output("Candidatos de paciente:")
        for index, candidate in enumerate(candidates, start=1):
            self.output(f"{index}. {candidate.nome} (PatientId {candidate.id})")
        selected = self._numbered_choice(
            "Selecione o número do paciente confirmado: ",
            candidates,
        )
        return ConfirmedPatient(selected.nome, selected.id)

    def _choose_patient_folder(self, patient_name: str) -> Path:
        matches = self.folder_locator.find_compatible(patient_name)
        if not matches:
            raise SupervisedImportError(
                "Nenhuma pasta compatível de paciente foi encontrada."
            )

        self.output("Pastas compatíveis no OneDrive local:")
        for index, folder in enumerate(matches, start=1):
            self.output(f"{index}. {folder}")
        selected = self._numbered_choice(
            "Selecione o número da pasta confirmada: ",
            matches,
        )
        return self.folder_locator.validate_selection(selected)

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
    ) -> None:
        self.output(f"Arquivo de origem: {archive}")
        self.output(f"Pasta extraída: {extracted}")
        self.output(f"Paciente confirmado: {patient.name}")
        self.output(
            "PatientId: "
            f"{patient.patient_id if patient.patient_id is not None else 'não disponível'}"
        )
        self.output(f"Pasta do paciente: {patient_folder}")
        self.output(f"Destino final: {destination}")
        self.output(f"Quantidade de arquivos: {file_count}")
        self.output(f"Tamanho total: {total_size} bytes")
        self.output(f"Possíveis duplicados: {duplicate_count}")

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
