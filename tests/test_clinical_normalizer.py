"""Contratos da normalização clínica multiformato da Sprint 16."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
import zipfile

from acquisition.base import AcquiredPackage, AcquisitionRequest
from acquisition.clinical_normalizer import ClinicalAssetNormalizer


def request() -> AcquisitionRequest:
    return AcquisitionRequest(
        provider_id="cfaz", request_id="27754597",
        provider_request_id="27754597", sequential_id="85871",
        clinic_number="30510", source_url="https://max.cfaz.net/requests/27754597",
        patient_name=None, request_date=None, exam_date=None,
        radiology_clinic=None, professional=None, assets=(),
    )


def jpeg(width=1200, height=800) -> bytes:
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = (
        b"\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    )
    return b"\xff\xd8" + app0 + sof + b"pixels\xff\xd9"


def package(tmp_path: Path, files: dict[str, bytes], metadata):
    archive = tmp_path / "provider.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for name, content in files.items():
            output.writestr(name, content)
    return AcquiredPackage(
        request=request(), archive_path=archive,
        sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        file_count=len(files), total_bytes=sum(map(len, files.values())),
        file_metadata=tuple(metadata),
    )


def test_normalizes_collections_names_extensions_and_deduplicates(tmp_path):
    image = jpeg()
    acquired = package(tmp_path, {
        "raw-a": image, "raw-b": image,
        "model": b"solid jaw\nfacet normal 0 0 1\nendfacet\nendsolid jaw\n",
        "unknown": b"\x00\x01\x02proprietary",
    }, (
        {"stored_name": "raw-a", "collection": "request.teleradiographies[1]"},
        {"stored_name": "raw-b", "collection": "request.images_download_links"},
        {"stored_name": "model", "collection": "request.dental_models[1]"},
        {"stored_name": "unknown", "collection": "request.archives[1]"},
    ))
    normalizer = ClinicalAssetNormalizer(
        now_provider=lambda: datetime(2026, 7, 24, tzinfo=timezone.utc)
    )

    normalized = normalizer.normalize(acquired)

    with zipfile.ZipFile(normalized.archive_path) as archive:
        assert sorted(archive.namelist()) == [
            "01 - Radiografias/telerradiografia_001.jpg",
            "04 - Modelos Digitais/modelo_digital_001.stl",
            "07 - Arquivos Auxiliares/arquivo_auxiliar_001.bin",
        ]
    assert len(normalized.file_metadata) == 4
    duplicate = next(
        item for item in normalized.file_metadata if item["is_duplicate"]
    )
    assert duplicate["is_duplicate"] is True
    assert duplicate["duplicate_of"] == (
        "01 - Radiografias/telerradiografia_001.jpg"
    )
    assert all("http" not in str(item) for item in normalized.file_metadata)
    radiograph = next(
        item for item in normalized.file_metadata
        if item["clinical_category"] == "RADIOGRAPH"
    )
    assert radiograph["width"] == 1200
    assert radiograph["detected_extension"] == ".jpg"


def test_detects_supported_formats_by_content_before_filename(tmp_path):
    fixtures = {
        "jpeg.bin": (jpeg(), "image/jpeg", ".jpg"),
        "png.bin": (
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
            + (10).to_bytes(4, "big") + (20).to_bytes(4, "big"),
            "image/png", ".png",
        ),
        "tiff.bin": (b"II*\x00" + b"\x00" * 100, "image/tiff", ".tif"),
        "bmp.bin": (b"BM" + b"\x00" * 100, "image/bmp", ".bmp"),
        "webp.bin": (b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 20, "image/webp", ".webp"),
        "pdf.bin": (b"%PDF-1.7\n", "application/pdf", ".pdf"),
        "dicom.bin": (b"\x00" * 128 + b"DICM" + b"\x00" * 20, "application/dicom", ".dcm"),
        "rar.bin": (b"Rar!\x1a\x07\x01\x00", "application/vnd.rar", ".rar"),
        "seven.bin": (b"7z\xbc\xaf'\x1c" + b"\x00" * 20, "application/x-7z-compressed", ".7z"),
        "obj.bin": (b"v 0 0 0\nv 1 0 0\n", "model/obj", ".obj"),
        "ply.bin": (b"ply\nformat ascii 1.0\nend_header\n", "model/ply", ".ply"),
        "json.bin": (b'{"ok": true}', "application/json", ".json"),
        "xml.bin": (b"<?xml version='1.0'?><root/>", "application/xml", ".xml"),
    }
    for name, (content, mime, extension) in fixtures.items():
        path = tmp_path / name
        path.write_bytes(content)
        detected = ClinicalAssetNormalizer.detect(path)
        assert (detected.mime, detected.extension) == (mime, extension)


def test_package_without_file_inventory_is_preserved_byte_for_byte(tmp_path):
    archive = tmp_path / "viewer.rar"
    archive.write_bytes(b"Rar!\x1a\x07\x01\x00viewer")
    acquired = AcquiredPackage(
        request=request(), archive_path=archive,
        sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        file_count=1, total_bytes=archive.stat().st_size,
    )

    normalized = ClinicalAssetNormalizer().normalize(acquired)

    assert normalized is acquired
    assert archive.read_bytes() == b"Rar!\x1a\x07\x01\x00viewer"
