"""Contrato provider-neutral entre aquisição e publicação clínica."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ClinicalAsset:
    asset_id: str
    provider_collection: str
    provider_section: str | None
    provider_display_name: str | None
    clinical_category: str
    mime_type: str
    extension: str
    sha256: str
    thumbnail: bool
    width: int | None
    height: int | None
    size: int
    relative_folder: str
    original_filename: str | None
    normalized_filename: str
    download_source: str | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    digital_model_id: str | None = None
    stl_file_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: dict[str, Any], *, provider: str = "") -> "ClinicalAsset":
        source = str(value.get("source_collection") or value.get("collection") or "unknown")
        filename = value.get("stored_name") or value.get("normalized_filename") or ""
        return cls(
            asset_id=str(value.get("asset_id") or value.get("sha256") or filename),
            provider_collection=source,
            provider_section=value.get("provider_section"),
            provider_display_name=value.get("provider_display_name"),
            clinical_category=str(value.get("clinical_category") or "UNKNOWN"),
            mime_type=str(value.get("detected_mime") or value.get("mime_type") or "application/octet-stream"),
            extension=str(value.get("detected_extension") or value.get("extension") or ""),
            sha256=str(value.get("sha256") or ""),
            thumbnail=bool(value.get("is_thumbnail", value.get("thumbnail", False))),
            width=value.get("width"), height=value.get("height"),
            size=int(value.get("size_bytes", value.get("size", 0)) or 0),
            relative_folder=str(value.get("relative_folder") or ""),
            original_filename=value.get("source_name") or value.get("original_filename"),
            normalized_filename=str(filename),
            download_source=value.get("download_source"),
            provider_metadata=dict(value.get("provider_metadata") or {
                key: value[key] for key in ("provider", "provider_id") if key in value
            }),
            digital_model_id=value.get("provider_exam_id") or value.get("digital_model_id"),
            stl_file_id=value.get("provider_asset_id") or value.get("stl_file_id"),
        )


@dataclass(frozen=True)
class ClinicalPackage:
    provider: str
    provider_request_id: str
    provider_internal_id: str | None
    sequential_id: str | None
    clinic_number: str | None
    patient: str | None
    exam_date: str | None
    provider_name: str | None
    assets: tuple[ClinicalAsset, ...] = ()
    manifest: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_manifest(self) -> dict[str, Any]:
        return {
            "schema_version": "16.2",
            "provider_schema_version": self.metadata.get("provider_schema_version"),
            "normalizer_version": self.manifest.get("normalizer_version"),
            "provider": self.provider,
            "request": {
                "provider_request_id": self.provider_request_id,
                "provider_internal_id": self.provider_internal_id,
                "sequential_id": self.sequential_id,
                "clinic_number": self.clinic_number,
            },
            "assets": [asset.to_dict() for asset in self.assets],
            "provider_metadata": dict(self.metadata),
        }
