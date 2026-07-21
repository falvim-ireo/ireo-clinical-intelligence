"""Orquestra o download e a identificação de um estudo radiológico."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
import logging
from pathlib import Path
from time import perf_counter

from models.patient import Patient
from radiology.dicom_reader import DicomReadError, DicomReader, DicomStudy
from radiology.patient_matcher import PatientMatchResult, PatientMatcher
from radiology.transfernow_download import (
    DownloadResult,
    TransferNowDownloadError,
    TransferNowDownloader,
)
from radiology.zip_extractor import (
    ExtractionResult,
    ZipExtractionError,
    ZipExtractor,
)


@dataclass(frozen=True)
class WorkflowResult:
    """Resultado completo ou parcial de uma execução do workflow."""

    success: bool
    duration_seconds: float
    download: DownloadResult | None
    extraction: ExtractionResult | None
    study: DicomStudy | None
    patient_match: PatientMatchResult | None
    errors: list[str]


class RadiologyWorkflow:
    """Executa sequencialmente download, extração, leitura e associação."""

    def __init__(
        self,
        downloader: TransferNowDownloader,
        zip_extractor: ZipExtractor,
        dicom_reader: DicomReader,
        patient_matcher: PatientMatcher | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Recebe as implementações concretas usadas em cada etapa."""

        self.downloader = downloader
        self.zip_extractor = zip_extractor
        self.dicom_reader = dicom_reader
        self.patient_matcher = patient_matcher or PatientMatcher()
        self.logger = logger or logging.getLogger(__name__)

    def run(
        self,
        *,
        transfer_url: str,
        original_filename: str | None,
        download_root: str | Path,
        correlation_id: str,
        message_id: str,
        patients: Collection[Patient],
    ) -> WorkflowResult:
        """Executa o pipeline e converte qualquer falha em ``WorkflowResult.errors``."""

        started_at = perf_counter()
        download: DownloadResult | None = None
        extraction: ExtractionResult | None = None
        study: DicomStudy | None = None
        patient_match: PatientMatchResult | None = None
        errors: list[str] = []
        stage = "download"

        self.logger.info("Workflow radiológico iniciado.")
        try:
            download = self.downloader.download(
                transfer_url,
                original_filename,
                download_root,
                correlation_id,
                message_id,
            )
            self.logger.info("Download TransferNow concluído.")

            stage = "extração ZIP"
            extraction = self.zip_extractor.extract(download.path)
            self.logger.info("Extração ZIP concluída.")

            stage = "leitura DICOM"
            study = self.dicom_reader.read(extraction)
            self.logger.info("Leitura DICOM concluída.")

            stage = "associação de paciente"
            patient_match = self.patient_matcher.match(study, patients)
            self.logger.info(
                "Associação de paciente concluída com status %s.",
                patient_match.status.value,
            )
        except Exception as exc:
            error = self._safe_error(stage, exc)
            errors.append(error)
            self.logger.error("Workflow radiológico falhou na etapa %s: %s", stage, error)

        duration = perf_counter() - started_at
        self.logger.info(
            "Workflow radiológico finalizado em %.6f segundos; sucesso=%s.",
            duration,
            not errors,
        )
        return WorkflowResult(
            success=not errors,
            duration_seconds=duration,
            download=download,
            extraction=extraction,
            study=study,
            patient_match=patient_match,
            errors=errors,
        )

    @staticmethod
    def _safe_error(stage: str, exc: Exception) -> str:
        """Produz erro útil sem propagar detalhes inesperados potencialmente sensíveis."""

        if isinstance(
            exc,
            (TransferNowDownloadError, ZipExtractionError, DicomReadError),
        ):
            detail = str(exc).strip() or "falha sem detalhes"
            return f"{stage}: {detail}"
        return f"{stage}: falha inesperada ({type(exc).__name__})."
