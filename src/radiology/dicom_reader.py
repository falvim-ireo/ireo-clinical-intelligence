"""Leitura estrutural segura de pacotes DICOM, sem interpretação clínica."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any

from pydicom import dcmread
from pydicom.dataset import Dataset
from pydicom.errors import InvalidDicomError

from radiology.zip_extractor import ExtractionResult
from services.patient_normalizer import PatientNormalizer


class DicomReadError(RuntimeError):
    """Indica que a extração não contém um estudo DICOM legível e consistente."""


@dataclass(frozen=True)
class EstimatedValue:
    """Valor geométrico derivado, com suas fontes explicitamente registradas."""

    value: tuple[float, ...]
    unit: str
    estimated: bool = True
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class DicomInstance:
    """Metadados não clínicos extraídos de uma instância DICOM."""

    path: str
    patient_name: str | None
    patient_id: str | None
    patient_birth_date: str | None
    patient_sex: str | None
    study_instance_uid: str | None
    series_instance_uid: str | None
    sop_instance_uid: str | None
    study_date: str | None
    study_time: str | None
    study_description: str | None
    series_description: str | None
    modality: str | None
    manufacturer: str | None
    manufacturer_model_name: str | None
    software_versions: str | None
    rows: int | None
    columns: int | None
    pixel_spacing: tuple[float, float] | None
    slice_thickness: float | None
    spacing_between_slices: float | None
    image_position_patient: tuple[float, ...] | None
    image_orientation_patient: tuple[float, ...] | None
    kvp: float | None
    exposure: dict[str, float]
    instance_number: int | None = None
    images_in_acquisition: int | None = None


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
    study_instance_uid: str | None = None
    study_date: str | None = None
    rows: int | None = None
    columns: int | None = None
    spacing_between_slices: float | None = None
    image_position_patient: tuple[float, ...] | None = None
    image_orientation_patient: tuple[float, ...] | None = None
    kvp: float | None = None
    exposure: dict[str, float] = field(default_factory=dict)
    estimated_voxel_size: EstimatedValue | None = None
    estimated_fov: EstimatedValue | None = None


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
    patient_sex: str | None = None
    study_time: str | None = None
    software_versions: str | None = None

    @property
    def series_count(self) -> int:
        return len(self.series)


@dataclass(frozen=True)
class DicomPackageAnalysis:
    """Inventário estrutural completo e serializável do pacote recebido."""

    valid_dicoms: tuple[DicomInstance, ...]
    invalid_dicoms: tuple[str, ...]
    pdfs: tuple[str, ...]
    conventional_images: tuple[str, ...]
    executable_viewers: tuple[str, ...]
    auxiliary_files: tuple[str, ...]
    non_dicom_files: tuple[str, ...]
    unrecognized_files: tuple[str, ...]
    proprietary_files: tuple[str, ...]
    studies: tuple[DicomStudy, ...]
    patient_keys: tuple[str, ...]
    probable_classification: str
    alerts: tuple[str, ...]

    @property
    def valid_dicom_count(self) -> int:
        return len(self.valid_dicoms)

    @property
    def study_count(self) -> int:
        return len(self.studies)

    @property
    def series_count(self) -> int:
        return sum(study.series_count for study in self.studies)

    @property
    def patient_count(self) -> int:
        return len(self.patient_keys)

    @property
    def requires_manual_review(self) -> bool:
        return self.patient_count > 1

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.update(
            valid_dicom_count=self.valid_dicom_count,
            study_count=self.study_count,
            series_count=self.series_count,
            patient_count=self.patient_count,
            requires_manual_review=self.requires_manual_review,
        )
        relative_files: dict[tuple[str | None, str | None], list[str]] = {}
        for instance in self.valid_dicoms:
            relative_files.setdefault(
                (instance.study_instance_uid, instance.series_instance_uid), []
            ).append(instance.path)
        for study in value["studies"]:
            for series in study["series"]:
                series["files"] = sorted(
                    relative_files.get(
                        (study["study_instance_uid"], series["series_instance_uid"]),
                        [],
                    )
                )
        return value


class DicomReader:
    """Lê metadados com ``stop_before_pixels=True`` e inventaria todo o pacote."""

    IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
    EXECUTABLE_SUFFIXES = {".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".jar"}
    PROPRIETARY_SUFFIXES = {".sl", ".vol", ".raw", ".dat", ".vgi", ".xstd"}
    REPORT_FILENAMES = {"dicom_summary.json", "resumo_do_exame.txt"}

    def read(self, source: ExtractionResult | str | Path) -> DicomStudy:
        """Mantém o contrato estrito legado de exatamente um estudo legível."""

        analysis = self.analyze(source)
        if not analysis.valid_dicoms:
            raise DicomReadError("Nenhuma imagem DICOM legível foi encontrada.")
        if len(analysis.studies) != 1:
            raise DicomReadError("A extração contém mais de um estudo DICOM.")
        study = analysis.studies[0]
        if study.study_instance_uid.startswith("MISSING-STUDY-"):
            raise DicomReadError("StudyInstanceUID ausente nas imagens DICOM.")
        if any(
            series.series_instance_uid.startswith("MISSING-SERIES-")
            for series in study.series
        ):
            raise DicomReadError("SeriesInstanceUID ausente em uma imagem DICOM.")
        return study

    def analyze(
        self,
        source: ExtractionResult | str | Path,
        *,
        confirmed_patient_name: str | None = None,
        confirmed_patient_id: str | int | None = None,
    ) -> DicomPackageAnalysis:
        """Classifica arquivos, agrupa DICOMs e produz alertas conservadores."""

        root, files = self._source_context(source)
        instances: list[tuple[Path, Dataset, DicomInstance]] = []
        invalid: list[str] = []
        pdfs: list[str] = []
        images: list[str] = []
        executables: list[str] = []
        auxiliary: list[str] = []
        non_dicom: list[str] = []
        unrecognized: list[str] = []
        proprietary: list[str] = []

        for path in files:
            relative = self._relative(path, root)
            if path.name.casefold() in self.REPORT_FILENAMES:
                continue
            suffix = path.suffix.casefold()
            if suffix == ".pdf" or self._starts_with(path, b"%PDF"):
                pdfs.append(relative)
                continue
            if suffix in self.IMAGE_SUFFIXES:
                images.append(relative)
                continue
            if suffix in self.EXECUTABLE_SUFFIXES:
                executables.append(relative)
                continue
            if suffix in self.PROPRIETARY_SUFFIXES:
                proprietary.append(relative)
                continue
            has_preamble = self._has_dicom_preamble(path)
            dicom_candidate = path.name.casefold() == "dicomdir" or has_preamble
            try:
                with path.open("rb") as stream:
                    dataset = dcmread(
                        stream, stop_before_pixels=True, force=False
                    )
            except (InvalidDicomError, OSError, ValueError, EOFError):
                if dicom_candidate:
                    invalid.append(relative)
                elif suffix == ".dcm" or not suffix:
                    unrecognized.append(relative)
                else:
                    non_dicom.append(relative)
                continue
            if not self._looks_like_dicom(dataset):
                (invalid if dicom_candidate else unrecognized).append(relative)
                continue
            instances.append((path, dataset, self._instance(relative, dataset)))

        studies = self._build_studies(instances)
        alerts = self._alerts(
            instances,
            studies,
            invalid,
            confirmed_patient_name,
            confirmed_patient_id,
        )
        patient_keys = tuple(sorted(self._patient_keys(item[2] for item in instances)))
        return DicomPackageAnalysis(
            valid_dicoms=tuple(item[2] for item in instances),
            invalid_dicoms=tuple(sorted(invalid)),
            pdfs=tuple(sorted(pdfs)),
            conventional_images=tuple(sorted(images)),
            executable_viewers=tuple(sorted(executables)),
            auxiliary_files=tuple(sorted(auxiliary)),
            non_dicom_files=tuple(sorted(non_dicom)),
            unrecognized_files=tuple(sorted(unrecognized)),
            proprietary_files=tuple(sorted(proprietary)),
            studies=tuple(studies),
            patient_keys=patient_keys,
            probable_classification=self._classify(instances),
            alerts=tuple(alerts),
        )

    def write_reports(
        self, analysis: DicomPackageAnalysis, destination: str | Path
    ) -> tuple[Path, Path]:
        """Gera relatórios determinísticos sem diagnóstico ou interpretação clínica."""

        root = Path(destination).resolve()
        if not root.is_dir():
            raise DicomReadError("O diretório para relatórios DICOM não existe.")
        summary_path = root / "dicom_summary.json"
        text_path = root / "resumo_do_exame.txt"
        try:
            summary_path.write_text(
                json.dumps(analysis.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            alert_text = "; ".join(analysis.alerts) if analysis.alerts else "nenhum"
            text_path.write_text(
                "Resumo estrutural do exame (sem interpretação clínica)\n"
                f"DICOM válidos: {analysis.valid_dicom_count}\n"
                f"Estudos: {analysis.study_count}\n"
                f"Séries: {analysis.series_count}\n"
                f"Pacientes encontrados: {analysis.patient_count}\n"
                f"Classificação provável: {analysis.probable_classification}\n"
                f"Alertas: {alert_text}\n",
                encoding="utf-8",
            )
        except OSError:
            raise DicomReadError("Não foi possível gerar os relatórios DICOM.") from None
        return summary_path, text_path

    @classmethod
    def _build_studies(
        cls, items: list[tuple[Path, Dataset, DicomInstance]]
    ) -> list[DicomStudy]:
        grouped: dict[str, list[tuple[Path, Dataset, DicomInstance]]] = {}
        for index, item in enumerate(items, start=1):
            key = item[2].study_instance_uid or f"MISSING-STUDY-{index}"
            grouped.setdefault(key, []).append(item)
        return [cls._build_study(uid, values) for uid, values in sorted(grouped.items())]

    @classmethod
    def _build_study(
        cls, study_uid: str, items: list[tuple[Path, Dataset, DicomInstance]]
    ) -> DicomStudy:
        series_grouped: dict[str, list[tuple[Path, Dataset, DicomInstance]]] = {}
        for index, item in enumerate(items, start=1):
            key = item[2].series_instance_uid or f"MISSING-SERIES-{index}"
            series_grouped.setdefault(key, []).append(item)
        series = [
            cls._build_series(uid, values)
            for uid, values in sorted(series_grouped.items())
        ]
        first = items[0][1]
        return DicomStudy(
            patient_name=cls._text(first, "PatientName"),
            patient_id=cls._text(first, "PatientID"),
            patient_birth_date=cls._text(first, "PatientBirthDate"),
            patient_sex=cls._text(first, "PatientSex"),
            study_date=cls._text(first, "StudyDate"),
            study_time=cls._text(first, "StudyTime"),
            study_description=cls._text(first, "StudyDescription"),
            study_instance_uid=study_uid,
            manufacturer=cls._text(first, "Manufacturer"),
            manufacturer_model_name=cls._text(first, "ManufacturerModelName"),
            software_versions=cls._text(first, "SoftwareVersions"),
            institution_name=cls._text(first, "InstitutionName"),
            modality=cls._text(first, "Modality"),
            series=series,
        )

    @classmethod
    def _build_series(
        cls, series_uid: str, items: list[tuple[Path, Dataset, DicomInstance]]
    ) -> DicomSeries:
        ordered = sorted(items, key=lambda item: item[0].as_posix().casefold())
        first = ordered[0][2]
        z_spacing = first.spacing_between_slices or first.slice_thickness
        voxel = None
        if first.pixel_spacing and z_spacing is not None:
            voxel = EstimatedValue(
                (*first.pixel_spacing, z_spacing),
                "mm",
                sources=("PixelSpacing", "SpacingBetweenSlices" if first.spacing_between_slices is not None else "SliceThickness"),
            )
        fov = None
        if first.pixel_spacing and first.rows and first.columns:
            values = [first.rows * first.pixel_spacing[0], first.columns * first.pixel_spacing[1]]
            sources = ["Rows", "Columns", "PixelSpacing"]
            positions = [item[2].image_position_patient for item in ordered]
            positions = [position for position in positions if position and len(position) >= 3]
            if len(positions) >= 2:
                values.append(max(position[2] for position in positions) - min(position[2] for position in positions) + (z_spacing or 0.0))
                sources.append("ImagePositionPatient")
            elif z_spacing is not None:
                values.append(z_spacing * len(ordered))
                sources.extend(("image_count", "SpacingBetweenSlices" if first.spacing_between_slices is not None else "SliceThickness"))
            fov = EstimatedValue(tuple(values), "mm", sources=tuple(sources))
        return DicomSeries(
            series_instance_uid=series_uid,
            study_instance_uid=first.study_instance_uid,
            study_date=first.study_date,
            modality=first.modality,
            series_description=first.series_description,
            slice_thickness=first.slice_thickness,
            pixel_spacing=first.pixel_spacing,
            rows=first.rows,
            columns=first.columns,
            spacing_between_slices=first.spacing_between_slices,
            image_position_patient=first.image_position_patient,
            image_orientation_patient=first.image_orientation_patient,
            kvp=first.kvp,
            exposure=first.exposure,
            estimated_voxel_size=voxel,
            estimated_fov=fov,
            image_count=len(ordered),
            files=[item[0] for item in ordered],
        )

    @classmethod
    def _instance(cls, relative: str, dataset: Dataset) -> DicomInstance:
        exposure = {
            name: value
            for name in ("Exposure", "ExposureTime", "XRayTubeCurrent", "ExposureInuAs")
            if (value := cls._float(dataset, name)) is not None
        }
        return DicomInstance(
            path=relative,
            patient_name=cls._text(dataset, "PatientName"),
            patient_id=cls._text(dataset, "PatientID"),
            patient_birth_date=cls._text(dataset, "PatientBirthDate"),
            patient_sex=cls._text(dataset, "PatientSex"),
            study_instance_uid=cls._text(dataset, "StudyInstanceUID"),
            series_instance_uid=cls._text(dataset, "SeriesInstanceUID"),
            sop_instance_uid=cls._text(dataset, "SOPInstanceUID"),
            study_date=cls._text(dataset, "StudyDate"),
            study_time=cls._text(dataset, "StudyTime"),
            study_description=cls._text(dataset, "StudyDescription"),
            series_description=cls._text(dataset, "SeriesDescription"),
            modality=cls._text(dataset, "Modality"),
            manufacturer=cls._text(dataset, "Manufacturer"),
            manufacturer_model_name=cls._text(dataset, "ManufacturerModelName"),
            software_versions=cls._text(dataset, "SoftwareVersions"),
            rows=cls._int(dataset, "Rows"),
            columns=cls._int(dataset, "Columns"),
            pixel_spacing=cls._pair(dataset, "PixelSpacing"),
            slice_thickness=cls._float(dataset, "SliceThickness"),
            spacing_between_slices=cls._float(dataset, "SpacingBetweenSlices"),
            image_position_patient=cls._vector(dataset, "ImagePositionPatient"),
            image_orientation_patient=cls._vector(dataset, "ImageOrientationPatient"),
            kvp=cls._float(dataset, "KVP"),
            exposure=exposure,
            instance_number=cls._int(dataset, "InstanceNumber"),
            images_in_acquisition=cls._int(dataset, "ImagesInAcquisition"),
        )

    @classmethod
    def _alerts(
        cls,
        items: list[tuple[Path, Dataset, DicomInstance]],
        studies: list[DicomStudy],
        invalid: list[str],
        confirmed_name: str | None,
        confirmed_id: str | int | None,
    ) -> list[str]:
        alerts: list[str] = []
        instances = [item[2] for item in items]
        patient_keys = cls._patient_keys(instances)
        if not instances:
            alerts.append("ausência de DICOM")
        if invalid:
            alerts.append(f"DICOM corrompidos: {len(invalid)}")
        if len(patient_keys) > 1:
            alerts.append("múltiplos pacientes")
        if len(studies) > 1:
            alerts.append("múltiplos estudos")
        sop_uids = [item.sop_instance_uid for item in instances if item.sop_instance_uid]
        if len(sop_uids) != len(set(sop_uids)):
            alerts.append("UIDs duplicados")
        required = {
            "PatientName": lambda item: item.patient_name,
            "StudyInstanceUID": lambda item: item.study_instance_uid,
            "SeriesInstanceUID": lambda item: item.series_instance_uid,
            "SOPInstanceUID": lambda item: item.sop_instance_uid,
            "Modality": lambda item: item.modality,
        }
        missing = sorted(
            name for name, getter in required.items() if any(not getter(item) for item in instances)
        )
        if missing:
            alerts.append("metadados ausentes: " + ", ".join(missing))
        if confirmed_name:
            expected = PatientNormalizer.compare_ready(confirmed_name)
            names = {
                PatientNormalizer.compare_ready(item.patient_name)
                for item in instances
                if item.patient_name
            }
            if names and expected not in names:
                alerts.append("nome incompatível com o paciente confirmado")
        if confirmed_id is not None:
            expected_id = str(confirmed_id).strip().casefold()
            ids = {str(item.patient_id).strip().casefold() for item in instances if item.patient_id}
            if ids and expected_id not in ids:
                alerts.append("PatientID incompatível com o paciente confirmado")
        for study in studies:
            for series in study.series:
                source = [item[2] for item in items if item[2].series_instance_uid == series.series_instance_uid]
                numbers = sorted({item.instance_number for item in source if item.instance_number is not None})
                expected_counts = [item.images_in_acquisition for item in source if item.images_in_acquisition]
                gap = len(numbers) >= 2 and numbers != list(range(numbers[0], numbers[-1] + 1))
                short = bool(expected_counts and max(expected_counts) > series.image_count)
                if gap or short:
                    alerts.append(f"série possivelmente incompleta: {series.series_instance_uid}")
        return alerts

    @classmethod
    def _classify(cls, items: list[tuple[Path, Dataset, DicomInstance]]) -> str:
        if not items:
            return "tipo não determinado"
        text = " ".join(
            filter(
                None,
                (
                    value
                    for item in items
                    for value in (
                        item[2].modality,
                        item[2].study_description,
                        item[2].series_description,
                        item[2].manufacturer_model_name,
                    )
                ),
            )
        ).upper()
        modalities = {item[2].modality for item in items}
        if any(marker in text for marker in ("CBCT", "CONE BEAM", "DENTAL", "ODONTO")):
            return "CBCT odontológica"
        if any(marker in text for marker in ("PANORAM", "PANORÂM")):
            return "panorâmica"
        if any(marker in text for marker in ("TELERRAD", "CEPH")):
            return "telerradiografia"
        if "PERIAPICAL" in text:
            return "periapical"
        if "CT" in modalities:
            return "CT médica"
        return "tipo não determinado"

    @staticmethod
    def _patient_keys(instances: Any) -> set[str]:
        return {
            "|".join(
                (
                    PatientNormalizer.compare_ready(item.patient_name),
                    str(item.patient_id or "").strip().casefold(),
                )
            )
            for item in instances
            if item.patient_name or item.patient_id
        }

    @staticmethod
    def _source_context(
        source: ExtractionResult | str | Path,
    ) -> tuple[Path, list[Path]]:
        if isinstance(source, ExtractionResult):
            root = source.destination.resolve()
            return root, sorted(path for path in source.files if path.is_file())
        root = Path(source).expanduser().resolve()
        if not root.is_dir():
            raise DicomReadError("O diretório da extração não existe.")
        return root, sorted(path for path in root.rglob("*") if path.is_file())

    @staticmethod
    def _relative(path: Path, root: Path) -> str:
        try:
            return path.resolve().relative_to(root).as_posix()
        except ValueError:
            return path.name

    @staticmethod
    def _starts_with(path: Path, prefix: bytes) -> bool:
        try:
            with path.open("rb") as stream:
                return stream.read(len(prefix)) == prefix
        except OSError:
            return False

    @staticmethod
    def _has_dicom_preamble(path: Path) -> bool:
        try:
            with path.open("rb") as stream:
                stream.seek(128)
                return stream.read(4) == b"DICM"
        except OSError:
            return False

    @staticmethod
    def _looks_like_dicom(dataset: Dataset) -> bool:
        return bool(
            getattr(dataset, "SOPClassUID", None)
            or getattr(dataset, "SOPInstanceUID", None)
            or getattr(dataset, "StudyInstanceUID", None)
        )

    @staticmethod
    def _text(dataset: Dataset, name: str) -> str | None:
        value: Any = getattr(dataset, name, None)
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @staticmethod
    def _float(dataset: Dataset, name: str) -> float | None:
        value: Any = getattr(dataset, name, None)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _int(cls, dataset: Dataset, name: str) -> int | None:
        value = cls._float(dataset, name)
        return int(value) if value is not None else None

    @classmethod
    def _pair(cls, dataset: Dataset, name: str) -> tuple[float, float] | None:
        value: Any = getattr(dataset, name, None)
        try:
            return (float(value[0]), float(value[1])) if len(value) == 2 else None
        except (TypeError, ValueError, IndexError):
            return None

    @classmethod
    def _vector(cls, dataset: Dataset, name: str) -> tuple[float, ...] | None:
        value: Any = getattr(dataset, name, None)
        try:
            return tuple(float(item) for item in value) if value is not None else None
        except (TypeError, ValueError):
            return None

    _pixel_spacing = classmethod(lambda cls, dataset: cls._pair(dataset, "PixelSpacing"))
