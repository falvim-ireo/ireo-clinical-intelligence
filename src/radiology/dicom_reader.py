"""Leitura de metadados DICOM e agrupamento de imagens por série."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydicom import dcmread
from pydicom.dataset import Dataset
from pydicom.errors import InvalidDicomError

from radiology.zip_extractor import ExtractionResult


class DicomReadError(RuntimeError):
    """Indica que a extração não contém um estudo DICOM legível e consistente."""


@dataclass(frozen=True)
class DicomSeries:
    """Metadados consolidados de uma série e seus arquivos DICOM."""

    series_instance_uid: str
    modality: str | None
    series_description: str | None
    slice_thickness: float | None
    pixel_spacing: tuple[float, float] | None
    image_count: int
    files: list[Path]


@dataclass(frozen=True)
class DicomStudy:
    """Metadados de um estudo e suas séries agrupadas."""

    patient_name: str | None
    patient_id: str | None
    study_date: str | None
    study_description: str | None
    study_instance_uid: str
    manufacturer: str | None
    manufacturer_model_name: str | None
    institution_name: str | None
    modality: str | None
    series: list[DicomSeries]
    patient_birth_date: str | None = None

    @property
    def series_count(self) -> int:
        """Retorna o número de séries do estudo."""

        return len(self.series)


class DicomReader:
    """Lê arquivos extraídos sem carregar dados de pixels em memória."""

    def read(self, source: ExtractionResult | str | Path) -> DicomStudy:
        """Lê uma extração ou diretório e retorna seu estudo consolidado."""

        files = self._source_files(source)
        datasets: list[tuple[Path, Dataset]] = []
        for path in files:
            if path.name.casefold() == "dicomdir":
                continue
            try:
                dataset = dcmread(path, stop_before_pixels=True, force=False)
            except (InvalidDicomError, OSError, ValueError):
                continue
            if self._text(dataset, "SOPInstanceUID") is None:
                continue
            datasets.append((path, dataset))

        if not datasets:
            raise DicomReadError("Nenhuma imagem DICOM legível foi encontrada.")

        study_uids = {
            uid
            for _, dataset in datasets
            if (uid := self._text(dataset, "StudyInstanceUID")) is not None
        }
        if not study_uids:
            raise DicomReadError("StudyInstanceUID ausente nas imagens DICOM.")
        if len(study_uids) != 1:
            raise DicomReadError("A extração contém mais de um estudo DICOM.")
        study_uid = next(iter(study_uids))

        grouped: dict[str, list[tuple[Path, Dataset]]] = {}
        for path, dataset in datasets:
            series_uid = self._text(dataset, "SeriesInstanceUID")
            if series_uid is None:
                raise DicomReadError(
                    "SeriesInstanceUID ausente em uma imagem DICOM."
                )
            grouped.setdefault(series_uid, []).append((path, dataset))

        series = [
            self._build_series(series_uid, items)
            for series_uid, items in sorted(grouped.items())
        ]
        first = datasets[0][1]
        return DicomStudy(
            patient_name=self._text(first, "PatientName"),
            patient_id=self._text(first, "PatientID"),
            study_date=self._text(first, "StudyDate"),
            study_description=self._text(first, "StudyDescription"),
            study_instance_uid=study_uid,
            manufacturer=self._text(first, "Manufacturer"),
            manufacturer_model_name=self._text(first, "ManufacturerModelName"),
            institution_name=self._text(first, "InstitutionName"),
            modality=self._text(first, "Modality"),
            series=series,
            patient_birth_date=self._text(first, "PatientBirthDate"),
        )

    @staticmethod
    def _source_files(source: ExtractionResult | str | Path) -> list[Path]:
        """Obtém candidatos a DICOM da lista extraída ou de um diretório."""

        if isinstance(source, ExtractionResult):
            return sorted(path for path in source.files if path.is_file())
        root = Path(source).expanduser().resolve()
        if not root.is_dir():
            raise DicomReadError("O diretório da extração não existe.")
        return sorted(path for path in root.rglob("*") if path.is_file())

    @classmethod
    def _build_series(
        cls,
        series_uid: str,
        items: list[tuple[Path, Dataset]],
    ) -> DicomSeries:
        """Consolida metadados e contagem de uma série."""

        ordered = sorted(items, key=lambda item: item[0].as_posix().casefold())
        first = ordered[0][1]
        return DicomSeries(
            series_instance_uid=series_uid,
            modality=cls._text(first, "Modality"),
            series_description=cls._text(first, "SeriesDescription"),
            slice_thickness=cls._float(first, "SliceThickness"),
            pixel_spacing=cls._pixel_spacing(first),
            image_count=len(ordered),
            files=[path for path, _ in ordered],
        )

    @staticmethod
    def _text(dataset: Dataset, name: str) -> str | None:
        """Normaliza um atributo textual opcional do dataset."""

        value: Any = getattr(dataset, name, None)
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @staticmethod
    def _float(dataset: Dataset, name: str) -> float | None:
        """Converte um atributo numérico opcional para ``float``."""

        value: Any = getattr(dataset, name, None)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _pixel_spacing(dataset: Dataset) -> tuple[float, float] | None:
        """Normaliza PixelSpacing como par numérico, quando disponível."""

        value: Any = getattr(dataset, "PixelSpacing", None)
        if value is None:
            return None
        try:
            if len(value) != 2:
                return None
            return float(value[0]), float(value[1])
        except (TypeError, ValueError, IndexError):
            return None
