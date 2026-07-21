"""Testes offline do extrator ZIP seguro."""

from __future__ import annotations

from pathlib import Path
import stat
import zipfile

import pytest

from radiology.zip_extractor import (
    ExtractionResult,
    ZipExtractionError,
    ZipExtractor,
)


def create_zip(path: Path, files: dict[str, bytes]) -> Path:
    """Cria um ZIP de teste com os membros informados."""

    with zipfile.ZipFile(path, mode="w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return path


def test_extracts_all_files_to_unique_temporary_folder(tmp_path: Path) -> None:
    archive = create_zip(
        tmp_path / "exam.zip",
        {
            "images/one.dcm": b"one",
            "images/two.dcm": b"two",
            "report.txt": b"report",
        },
    )
    temporary_root = tmp_path / "tmp"
    extractor = ZipExtractor(temporary_root)

    first = extractor.extract(archive)
    second = extractor.extract(archive)

    assert isinstance(first, ExtractionResult)
    assert first.destination.parent == temporary_root.resolve()
    assert first.destination != second.destination
    assert [path.relative_to(first.destination).as_posix() for path in first.files] == [
        "images/one.dcm",
        "images/two.dcm",
        "report.txt",
    ]
    assert all(path.is_file() for path in first.files)
    assert first.dicomdir is None


def test_locates_dicomdir_case_insensitively(tmp_path: Path) -> None:
    archive = create_zip(
        tmp_path / "dicom.zip",
        {"media/DiCoMdIr": b"index", "media/scan.dcm": b"scan"},
    )

    result = ZipExtractor(tmp_path / "tmp").extract(archive)

    assert result.dicomdir is not None
    assert result.dicomdir.name == "DiCoMdIr"
    assert result.dicomdir.read_bytes() == b"index"
    assert result.dicomdir in result.files


def test_rejects_missing_zip(tmp_path: Path) -> None:
    with pytest.raises(ZipExtractionError, match="não existe"):
        ZipExtractor(tmp_path / "tmp").extract(tmp_path / "missing.zip")


def test_rejects_invalid_zip(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.zip"
    invalid.write_bytes(b"not a zip")

    with pytest.raises(ZipExtractionError, match="ZIP válido"):
        ZipExtractor(tmp_path / "tmp").extract(invalid)


@pytest.mark.parametrize("unsafe_name", ["../outside.txt", r"..\outside.txt", "/tmp/outside.txt"])
def test_rejects_zip_slip_and_cleans_partial_destination(
    tmp_path: Path, unsafe_name: str
) -> None:
    archive = create_zip(
        tmp_path / "unsafe.zip",
        {"safe.txt": b"safe", unsafe_name: b"outside"},
    )
    temporary_root = tmp_path / "tmp"

    with pytest.raises(ZipExtractionError, match="caminho inseguro"):
        ZipExtractor(temporary_root).extract(archive)

    assert list(temporary_root.iterdir()) == []
    assert not (tmp_path / "outside.txt").exists()


def test_rejects_symbolic_link_member(tmp_path: Path) -> None:
    archive = tmp_path / "symlink.zip"
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, mode="w") as compressed:
        compressed.writestr(link, "target")

    with pytest.raises(ZipExtractionError, match="simbólicos"):
        ZipExtractor(tmp_path / "tmp").extract(archive)
