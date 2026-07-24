"""Normalização clínica, segura e independente do provedor de aquisição."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
from typing import Any, Callable
import zipfile

from acquisition.base import AcquiredPackage, AcquisitionError


class ClinicalCategory(StrEnum):
    RADIOGRAPH = "RADIOGRAPH"
    PHOTOGRAPH = "PHOTOGRAPH"
    TOMOGRAPHY = "TOMOGRAPHY"
    DIGITAL_MODEL = "DIGITAL_MODEL"
    REPORT = "REPORT"
    DOCUMENTATION = "DOCUMENTATION"
    AUXILIARY = "AUXILIARY"
    UNKNOWN = "UNKNOWN"


CLINICAL_FOLDERS = {
    ClinicalCategory.RADIOGRAPH: "01 - Radiografias",
    ClinicalCategory.PHOTOGRAPH: "02 - Fotografias",
    ClinicalCategory.TOMOGRAPHY: "03 - Tomografia",
    ClinicalCategory.DIGITAL_MODEL: "04 - Modelos Digitais",
    ClinicalCategory.REPORT: "05 - Laudos",
    ClinicalCategory.DOCUMENTATION: "06 - Documentação",
    ClinicalCategory.AUXILIARY: "07 - Arquivos Auxiliares",
    ClinicalCategory.UNKNOWN: "07 - Arquivos Auxiliares",
}


@dataclass(frozen=True)
class ClinicalAsset:
    provider: str
    provider_request_id: str
    sequential_id: str | None
    source_collection: str
    source_name: str | None
    source_url_hash: str | None
    stored_name: str
    relative_folder: str
    detected_mime: str
    detected_extension: str
    asset_type: str
    clinical_category: str
    width: int | None
    height: int | None
    size_bytes: int
    sha256: str
    is_thumbnail: bool
    is_duplicate: bool
    duplicate_of: str | None
    downloaded_at: str | None
    normalized_at: str
    normalization_action: str = "PRESERVED"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ClinicalPackage:
    archive_path: Path
    sha256: str
    assets: tuple[ClinicalAsset, ...]
    normalizer_version: str
    normalized_at: str


@dataclass(frozen=True)
class DetectedFormat:
    mime: str
    extension: str
    asset_type: str
    width: int | None = None
    height: int | None = None


class ClinicalAssetNormalizer:
    """Converte pacotes planos em uma organização clínica determinística."""

    VERSION = "16.0"
    MAX_INSPECTION_BYTES = 2 * 1024 * 1024

    def __init__(
        self, *, now_provider: Callable[[], datetime] | None = None
    ) -> None:
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def normalize(self, acquired: AcquiredPackage) -> AcquiredPackage:
        # Pacotes sem inventário por arquivo podem conter viewers e relações
        # internas. Eles permanecem integralmente intactos.
        if not getattr(acquired, "file_metadata", ()):
            return acquired
        archive = Path(acquired.archive_path)
        if not zipfile.is_zipfile(archive):
            return acquired
        normalized = self._normalize_zip(acquired)
        metadata = tuple(asset.to_dict() for asset in normalized.assets)
        return replace(
            acquired,
            archive_path=normalized.archive_path,
            sha256=normalized.sha256,
            file_count=sum(not asset.is_duplicate for asset in normalized.assets),
            total_bytes=sum(
                asset.size_bytes for asset in normalized.assets if not asset.is_duplicate
            ),
            file_metadata=metadata,
        )

    def _normalize_zip(self, acquired: AcquiredPackage) -> ClinicalPackage:
        archive = Path(acquired.archive_path)
        work = archive.parent / f".{archive.stem}-clinical-v16"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        metadata_by_name = {
            str(item.get("stored_name") or ""): dict(item)
            for item in acquired.file_metadata if isinstance(item, dict)
        }
        timestamp = self._utc(self.now_provider())
        assets: list[ClinicalAsset] = []
        counters: dict[tuple[ClinicalCategory, str], int] = {}
        seen_sha: dict[str, str] = {}
        try:
            with zipfile.ZipFile(archive) as source:
                entries = [item for item in source.infolist() if not item.is_dir()]
                for entry in sorted(entries, key=lambda item: item.filename.casefold()):
                    relative = PurePosixPath(entry.filename)
                    if (
                        relative.is_absolute()
                        or ".." in relative.parts
                        or "\\" in entry.filename
                    ):
                        raise AcquisitionError(
                            "Pacote adquirido contém caminho inseguro."
                        )
                    target = work / relative.name
                    with source.open(entry) as input_stream, target.open("xb") as output:
                        shutil.copyfileobj(input_stream, output, 1024 * 1024)
                    source_meta = metadata_by_name.get(relative.as_posix())
                    if source_meta is None:
                        source_meta = metadata_by_name.get(relative.name, {})
                    detected = self.detect(
                        target,
                        content_type=self._optional_text(
                            source_meta.get("detected_mime")
                        ),
                        source_name=(
                            self._optional_text(source_meta.get("source_name"))
                            or relative.name
                        ),
                    )
                    digest = self._sha256(target)
                    source_collection = str(
                        source_meta.get("collection")
                        or source_meta.get("source_collection")
                        or source_meta.get("original_source")
                        or "unknown"
                    )
                    category, subtype = self._category(
                        source_collection, detected, relative.name
                    )
                    folder = CLINICAL_FOLDERS[category]
                    if category == ClinicalCategory.REPORT and (
                        "associated_images" in source_collection
                        or "associated_images_download_links" in source_collection
                    ):
                        folder += "/Imagens associadas"
                    is_duplicate = digest in seen_sha
                    if is_duplicate:
                        stored_name = seen_sha[digest].rsplit("/", 1)[-1]
                        action = "DUPLICATE_SKIPPED"
                    else:
                        stored_name = self._stored_name(
                            category, subtype, detected.extension, counters
                        )
                        destination = work / folder / stored_name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        target.replace(destination)
                        seen_sha[digest] = f"{folder}/{stored_name}"
                        action = "NORMALIZED"
                    width, height = detected.width, detected.height
                    thumbnail = bool(
                        source_meta.get("is_thumbnail")
                        or (
                            width and height and max(width, height) <= 320
                            and self._thumbnail_hint(source_collection, relative.name)
                        )
                    )
                    assets.append(ClinicalAsset(
                        provider=acquired.request.provider_id,
                        provider_request_id=(
                            acquired.request.provider_request_id
                            or acquired.request.request_id
                        ),
                        sequential_id=acquired.request.sequential_id,
                        source_collection=source_collection,
                        source_name=relative.name,
                        source_url_hash=self._source_hash(source_meta),
                        stored_name=stored_name,
                        relative_folder=folder,
                        detected_mime=detected.mime,
                        detected_extension=detected.extension,
                        asset_type=detected.asset_type,
                        clinical_category=category.value,
                        width=width,
                        height=height,
                        size_bytes=entry.file_size,
                        sha256=digest,
                        is_thumbnail=thumbnail,
                        is_duplicate=is_duplicate,
                        duplicate_of=seen_sha.get(digest) if is_duplicate else None,
                        downloaded_at=self._optional_text(source_meta.get("downloaded_at")),
                        normalized_at=timestamp,
                        normalization_action=action,
                    ))
                    target.unlink(missing_ok=True)
            output_archive = archive.with_name(f"{archive.stem}-clinical.zip")
            temporary = output_archive.with_suffix(".zip.tmp")
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED
            ) as output:
                for path in sorted(work.rglob("*")):
                    if path.is_file():
                        info = zipfile.ZipInfo(path.relative_to(work).as_posix())
                        info.date_time = (1980, 1, 1, 0, 0, 0)
                        info.external_attr = 0o600 << 16
                        output.writestr(
                            info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED
                        )
            temporary.replace(output_archive)
            return ClinicalPackage(
                archive_path=output_archive,
                sha256=self._sha256(output_archive),
                assets=tuple(assets),
                normalizer_version=self.VERSION,
                normalized_at=timestamp,
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)

    @classmethod
    def detect(
        cls, path: str | Path, *, content_type: str | None = None,
        content_disposition: str | None = None, source_name: str | None = None,
    ) -> DetectedFormat:
        file_path = Path(path)
        data = file_path.read_bytes()[: cls.MAX_INSPECTION_BYTES]
        name = cls._name_from_disposition(content_disposition) or source_name or file_path.name
        lower = data[:512].lstrip().lower()
        signatures = (
            (b"\xff\xd8\xff", "image/jpeg", ".jpg", "IMAGE"),
            (b"\x89PNG\r\n\x1a\n", "image/png", ".png", "IMAGE"),
            (b"II*\x00", "image/tiff", ".tif", "IMAGE"),
            (b"MM\x00*", "image/tiff", ".tif", "IMAGE"),
            (b"BM", "image/bmp", ".bmp", "IMAGE"),
            (b"%PDF-", "application/pdf", ".pdf", "DOCUMENT"),
            (b"Rar!\x1a\x07", "application/vnd.rar", ".rar", "ARCHIVE"),
            (b"7z\xbc\xaf'\x1c", "application/x-7z-compressed", ".7z", "ARCHIVE"),
        )
        for signature, mime, extension, asset_type in signatures:
            if data.startswith(signature):
                width, height = cls._dimensions(data, mime)
                return DetectedFormat(mime, extension, asset_type, width, height)
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            width, height = cls._dimensions(data, "image/webp")
            return DetectedFormat("image/webp", ".webp", "IMAGE", width, height)
        if len(data) > 132 and data[128:132] == b"DICM":
            return DetectedFormat("application/dicom", ".dcm", "DICOM")
        if cls._is_structural_dicom(file_path):
            return DetectedFormat("application/dicom", ".dcm", "DICOM")
        suffix = Path(name).suffix.casefold()
        if data.startswith(b"PK\x03\x04"):
            if suffix == ".3mf" or cls._zip_contains_3mf(file_path):
                return DetectedFormat(
                    "model/3mf", ".3mf", "DIGITAL_MODEL"
                )
            return DetectedFormat("application/zip", ".zip", "ARCHIVE")
        text = data.decode("utf-8", errors="ignore").lstrip("\ufeff \t\r\n")
        if cls._looks_like_ascii_stl(text):
            return DetectedFormat("model/stl", ".stl", "DIGITAL_MODEL")
        if cls._looks_like_binary_stl(data, file_path.stat().st_size):
            return DetectedFormat("model/stl", ".stl", "DIGITAL_MODEL")
        if re.match(r"(?is)^(?:#.*\n)*\s*(?:v|o|g|mtllib)\s+", text):
            return DetectedFormat("model/obj", ".obj", "DIGITAL_MODEL")
        if text.casefold().startswith("ply\n") or text.casefold().startswith("ply\r\n"):
            return DetectedFormat("model/ply", ".ply", "DIGITAL_MODEL")
        if text:
            if text.startswith(("{", "[")):
                try:
                    json.loads(text)
                    return DetectedFormat("application/json", ".json", "DOCUMENT")
                except ValueError:
                    pass
            if text.startswith("<?xml") or re.match(r"^<[A-Za-z_:][^>]*>", text):
                return DetectedFormat("application/xml", ".xml", "DOCUMENT")
            if suffix == ".csv" and ("," in text or ";" in text):
                return DetectedFormat("text/csv", ".csv", "DOCUMENT")
            if cls._mostly_text(data):
                return DetectedFormat("text/plain", ".txt", "DOCUMENT")
        fallback = {
            ".vol": ("application/x-vol", "VOLUME"),
            ".pak": ("application/x-pak", "PACKAGE_DEPENDENCY"),
            ".dll": ("application/vnd.microsoft.portable-executable", "PACKAGE_DEPENDENCY"),
        }
        if suffix in fallback:
            mime, asset_type = fallback[suffix]
            return DetectedFormat(mime, suffix, asset_type)
        type_value = str(content_type or "").split(";", 1)[0].strip().casefold()
        content_types = {
            "image/jpeg": (".jpg", "IMAGE"), "image/png": (".png", "IMAGE"),
            "image/tiff": (".tif", "IMAGE"), "image/bmp": (".bmp", "IMAGE"),
            "image/webp": (".webp", "IMAGE"), "application/pdf": (".pdf", "DOCUMENT"),
            "application/dicom": (".dcm", "DICOM"),
        }
        if type_value in content_types:
            extension, asset_type = content_types[type_value]
            return DetectedFormat(type_value, extension, asset_type)
        known_suffixes = {
            ".stl": ("model/stl", "DIGITAL_MODEL"),
            ".obj": ("model/obj", "DIGITAL_MODEL"),
            ".ply": ("model/ply", "DIGITAL_MODEL"),
            ".3mf": ("model/3mf", "DIGITAL_MODEL"),
            ".dcm": ("application/dicom", "DICOM"),
            ".zip": ("application/zip", "ARCHIVE"),
            ".rar": ("application/vnd.rar", "ARCHIVE"),
            ".7z": ("application/x-7z-compressed", "ARCHIVE"),
        }
        if suffix in known_suffixes:
            mime, asset_type = known_suffixes[suffix]
            return DetectedFormat(mime, suffix, asset_type)
        return DetectedFormat("application/octet-stream", suffix or ".bin", "UNKNOWN")

    @staticmethod
    def _category(
        source: str, detected: DetectedFormat, name: str
    ) -> tuple[ClinicalCategory, str]:
        value = f"{source} {name}".casefold()
        if "associated_images" in value or "reports" in value or "laudo" in value:
            return ClinicalCategory.REPORT, "REPORT_ASSOCIATED_IMAGE"
        if "tomograph" in value or detected.asset_type == "DICOM":
            return ClinicalCategory.TOMOGRAPHY, "DICOM_SERIES"
        if any(term in value for term in ("dental_models", "digital_models")) or (
            detected.asset_type == "DIGITAL_MODEL"
        ):
            subtype = "STL_MAXILLA" if "maxil" in value else (
                "STL_MANDIBLE" if "mandib" in value else "DIGITAL_MODEL"
            )
            return ClinicalCategory.DIGITAL_MODEL, subtype
        if any(term in value for term in ("frontal_facials", "lateral_facials", "photo")):
            subtype = "FACIAL_PROFILE" if "lateral" in value else "FACIAL_FRONTAL"
            return ClinicalCategory.PHOTOGRAPH, subtype
        if any(term in value for term in (
            "teleradiograph", "frontals", "carpals", "panoramic",
            "periap", "bitewing", "radiograph",
        )):
            subtype = "CEPHALOMETRIC" if "teleradiograph" in value else (
                "PANORAMIC" if "panoramic" in value else "RADIOGRAPH"
            )
            return ClinicalCategory.RADIOGRAPH, subtype
        if "implant" in value:
            return ClinicalCategory.DOCUMENTATION, "DOCUMENTATION"
        if detected.mime == "application/pdf":
            return ClinicalCategory.DOCUMENTATION, "DOCUMENTATION"
        if detected.asset_type in {"IMAGE", "DOCUMENT"}:
            return ClinicalCategory.DOCUMENTATION, "DOCUMENTATION"
        if detected.asset_type in {"ARCHIVE", "VOLUME", "PACKAGE_DEPENDENCY"}:
            return ClinicalCategory.AUXILIARY, "AUXILIARY"
        return ClinicalCategory.UNKNOWN, "AUXILIARY"

    @staticmethod
    def _stored_name(
        category: ClinicalCategory, subtype: str, extension: str,
        counters: dict[tuple[ClinicalCategory, str], int],
    ) -> str:
        prefixes = {
            "PANORAMIC": "panoramica", "CEPHALOMETRIC": "telerradiografia",
            "FACIAL_FRONTAL": "foto_facial_frontal",
            "FACIAL_PROFILE": "foto_facial_perfil",
            "STL_MAXILLA": "modelo_maxila", "STL_MANDIBLE": "modelo_mandibula",
            "DIGITAL_MODEL": "modelo_digital",
            "REPORT_ASSOCIATED_IMAGE": "imagem_associada_laudo",
            "DICOM_SERIES": "dicom",
            "DOCUMENTATION": "documentacao", "AUXILIARY": "arquivo_auxiliar",
        }
        defaults = {
            ClinicalCategory.RADIOGRAPH: "radiografia",
            ClinicalCategory.PHOTOGRAPH: "fotografia",
            ClinicalCategory.TOMOGRAPHY: "tomografia",
            ClinicalCategory.DIGITAL_MODEL: "modelo_digital",
            ClinicalCategory.REPORT: "laudo",
            ClinicalCategory.DOCUMENTATION: "documentacao",
            ClinicalCategory.AUXILIARY: "arquivo_auxiliar",
            ClinicalCategory.UNKNOWN: "arquivo_auxiliar",
        }
        prefix = prefixes.get(subtype, defaults[category])
        key = (category, prefix)
        counters[key] = counters.get(key, 0) + 1
        return f"{prefix}_{counters[key]:03d}{extension}"

    @staticmethod
    def _dimensions(data: bytes, mime: str) -> tuple[int | None, int | None]:
        if mime == "image/png" and len(data) >= 24:
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if mime == "image/bmp" and len(data) >= 26:
            return int.from_bytes(data[18:22], "little"), abs(
                int.from_bytes(data[22:26], "little", signed=True)
            )
        if mime == "image/jpeg":
            offset = 2
            sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                   0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
            while offset + 9 < len(data):
                if data[offset] != 0xFF:
                    offset += 1
                    continue
                marker = data[offset + 1]
                offset += 2
                if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                    continue
                length = int.from_bytes(data[offset:offset + 2], "big")
                if length < 2 or offset + length > len(data):
                    break
                if marker in sof:
                    return (
                        int.from_bytes(data[offset + 5:offset + 7], "big"),
                        int.from_bytes(data[offset + 3:offset + 5], "big"),
                    )
                offset += length
        return None, None

    @staticmethod
    def _looks_like_ascii_stl(text: str) -> bool:
        value = text[:200000].casefold()
        return value.startswith("solid") and "facet normal" in value and "endsolid" in value

    @staticmethod
    def _looks_like_binary_stl(data: bytes, total_size: int) -> bool:
        if len(data) < 84:
            return False
        triangles = int.from_bytes(data[80:84], "little")
        return triangles > 0 and 84 + triangles * 50 == total_size

    @staticmethod
    def _zip_contains_3mf(path: Path) -> bool:
        try:
            with zipfile.ZipFile(path) as archive:
                names = {name.casefold() for name in archive.namelist()}
            return "[content_types].xml" in names and any(
                name.startswith("3d/") and name.endswith(".model") for name in names
            )
        except (OSError, zipfile.BadZipFile):
            return False

    @staticmethod
    def _is_structural_dicom(path: Path) -> bool:
        try:
            import pydicom
            dataset = pydicom.dcmread(
                str(path), stop_before_pixels=True, force=True,
                specific_tags=[
                    "SOPClassUID", "SOPInstanceUID", "StudyInstanceUID",
                    "SeriesInstanceUID",
                ],
            )
        except Exception:
            return False
        return bool(
            getattr(dataset, "SOPClassUID", None)
            and (
                getattr(dataset, "SOPInstanceUID", None)
                or getattr(dataset, "StudyInstanceUID", None)
            )
        )

    @staticmethod
    def _mostly_text(data: bytes) -> bool:
        if not data or b"\x00" in data:
            return False
        printable = sum(byte in b"\t\n\r" or 32 <= byte <= 126 or byte >= 128 for byte in data)
        return printable / len(data) >= 0.95

    @staticmethod
    def _thumbnail_hint(source: str, name: str) -> bool:
        value = f"{source} {name}".casefold()
        return any(term in value for term in ("thumb", "thumbnail", "preview", "miniatura"))

    @staticmethod
    def _source_hash(metadata: dict[str, Any]) -> str | None:
        value = metadata.get("source_url_hash")
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
            return value
        # Só hashes já sanitizados são aceitos; URLs completas nunca são copiadas.
        return None

    @staticmethod
    def _name_from_disposition(value: str | None) -> str | None:
        if not value:
            return None
        match = re.search(r"filename\\*?=(?:UTF-8''|\"?)([^\";]+)", value, re.I)
        return match.group(1).strip() if match else None

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _utc(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
