"""Contratos comuns entre provedores e o pipeline clínico existente."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class AcquisitionError(RuntimeError):
    """Erro sanitizado de aquisição, sem credenciais ou URLs assinadas."""


class AssetClassification(StrEnum):
    IMAGE = "Imagem"
    REPORT_ASSOCIATED_IMAGE = "Imagem associada ao laudo"
    PANORAMIC = "Panorâmica"
    TELERADIOGRAPHY = "Telerradiografia"
    PERIAPICAL_SERIES = "Série Periapical"
    BITE_WING = "Bite-wing"
    CLINICAL_PHOTO = "Fotografia Clínica"
    REPORT = "Laudo"
    AUXILIARY_DOCUMENT = "Documento auxiliar"
    OTHER = "Outro"


@dataclass(frozen=True)
class AcquisitionAsset:
    asset_id: str
    download_url: str
    filename: str | None
    classification: AssetClassification
    source_field: str | None = None
    probe_html: bool = False


@dataclass(frozen=True)
class AcquisitionRequest:
    provider_id: str
    request_id: str
    source_url: str
    patient_name: str | None
    request_date: datetime | None
    exam_date: datetime | None
    radiology_clinic: str | None
    professional: str | None
    assets: tuple[AcquisitionAsset, ...]
    provider_exam_id: str | None = None
    provider_request_id: str | None = None
    sequential_id: str | None = None
    clinic_number: str | None = None

    def manifest_metadata(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "request_id": self.request_id,
            "provider_request_id": self.provider_request_id or self.request_id,
            "sequential_id": self.sequential_id,
            "clinic_number": self.clinic_number,
            "provider_exam_id": self.provider_exam_id,
            "request_date": self.request_date.isoformat() if self.request_date else None,
            "exam_date": self.exam_date.isoformat() if self.exam_date else None,
            "patient_name": self.patient_name,
            "radiology_clinic": self.radiology_clinic,
            "professional": self.professional,
            "source_url": self.source_url,
            "classifications": sorted({asset.classification.value for asset in self.assets}),
            "asset_count": len(self.assets),
        }


@dataclass(frozen=True)
class AcquiredPackage:
    request: AcquisitionRequest
    archive_path: Path
    sha256: str
    file_count: int
    total_bytes: int
    resumed_files: int = 0
    file_metadata: tuple[dict[str, Any], ...] = ()


class AcquisitionProvider(ABC):
    provider_id: str
    provider_name: str

    @abstractmethod
    def authenticate(self) -> None: ...

    @abstractmethod
    def discover(self, notification: Any) -> tuple[AcquisitionRequest, ...]: ...

    @abstractmethod
    def download(
        self, request: AcquisitionRequest, quarantine_root: str | Path,
        correlation_id: str,
    ) -> AcquiredPackage: ...

    @abstractmethod
    def classify(self, filename: str, metadata: dict[str, Any] | None = None) -> AssetClassification: ...

    @abstractmethod
    def finalize(self, request: AcquisitionRequest, *, success: bool) -> None: ...
