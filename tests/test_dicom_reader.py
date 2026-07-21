"""Testes do leitor e agrupador de estudos DICOM."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

from radiology.dicom_reader import DicomReadError, DicomReader
from radiology.zip_extractor import ExtractionResult


def write_dicom(
    path: Path,
    *,
    study_uid: str,
    series_uid: str,
    modality: str = "CT",
    series_description: str = "Axial",
) -> Path:
    """Grava uma instância DICOM mínima e válida para os testes."""

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = CTImageStorage
    dataset.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    dataset.PatientName = "PACIENTE^TESTE"
    dataset.PatientID = "P-123"
    dataset.StudyDate = "20260721"
    dataset.StudyDescription = "Tórax"
    dataset.StudyInstanceUID = study_uid
    dataset.Manufacturer = "Fabricante"
    dataset.ManufacturerModelName = "Modelo X"
    dataset.InstitutionName = "IREO"
    dataset.Modality = modality
    dataset.SeriesInstanceUID = series_uid
    dataset.SeriesDescription = series_description
    dataset.SliceThickness = "1.25"
    dataset.PixelSpacing = ["0.7", "0.8"]
    path.parent.mkdir(parents=True, exist_ok=True)
    dataset.save_as(path, enforce_file_format=True)
    return path


def test_reads_study_and_groups_images_by_series(tmp_path: Path) -> None:
    study_uid = generate_uid()
    first_series_uid = generate_uid()
    second_series_uid = generate_uid()
    write_dicom(
        tmp_path / "series-1" / "image-1.dcm",
        study_uid=study_uid,
        series_uid=first_series_uid,
    )
    write_dicom(
        tmp_path / "series-1" / "image-2.dcm",
        study_uid=study_uid,
        series_uid=first_series_uid,
    )
    write_dicom(
        tmp_path / "series-2" / "image-1.dcm",
        study_uid=study_uid,
        series_uid=second_series_uid,
        modality="MR",
        series_description="Coronal",
    )

    study = DicomReader().read(tmp_path)

    assert study.patient_name == "PACIENTE^TESTE"
    assert study.patient_id == "P-123"
    assert study.study_date == "20260721"
    assert study.study_description == "Tórax"
    assert study.study_instance_uid == study_uid
    assert study.manufacturer == "Fabricante"
    assert study.manufacturer_model_name == "Modelo X"
    assert study.institution_name == "IREO"
    assert study.modality == "CT"
    assert study.series_count == 2
    by_uid = {series.series_instance_uid: series for series in study.series}
    assert by_uid[first_series_uid].image_count == 2
    assert by_uid[first_series_uid].series_description == "Axial"
    assert by_uid[first_series_uid].slice_thickness == 1.25
    assert by_uid[first_series_uid].pixel_spacing == (0.7, 0.8)
    assert by_uid[second_series_uid].image_count == 1
    assert by_uid[second_series_uid].modality == "MR"


def test_reads_extraction_result_with_dicomdir(tmp_path: Path) -> None:
    study_uid = generate_uid()
    image = write_dicom(
        tmp_path / "images" / "image.dcm",
        study_uid=study_uid,
        series_uid=generate_uid(),
    )
    dicomdir = tmp_path / "DICOMDIR"
    dicomdir.write_bytes(b"not required for traversal")
    result = ExtractionResult(tmp_path, [dicomdir, image], dicomdir)

    study = DicomReader().read(result)

    assert study.study_instance_uid == study_uid
    assert study.series_count == 1
    assert study.series[0].files == [image]


def test_reads_directory_without_dicomdir_and_ignores_non_dicom(tmp_path: Path) -> None:
    image = write_dicom(
        tmp_path / "image-without-extension",
        study_uid=generate_uid(),
        series_uid=generate_uid(),
    )
    (tmp_path / "notes.txt").write_text("not dicom", encoding="utf-8")

    study = DicomReader().read(tmp_path)

    assert study.series[0].files == [image]
    assert study.series[0].image_count == 1


def test_rejects_directory_without_dicom_images(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("not dicom", encoding="utf-8")

    with pytest.raises(DicomReadError, match="Nenhuma imagem"):
        DicomReader().read(tmp_path)


def test_rejects_multiple_studies_in_same_extraction(tmp_path: Path) -> None:
    write_dicom(
        tmp_path / "one.dcm",
        study_uid=generate_uid(),
        series_uid=generate_uid(),
    )
    write_dicom(
        tmp_path / "two.dcm",
        study_uid=generate_uid(),
        series_uid=generate_uid(),
    )

    with pytest.raises(DicomReadError, match="mais de um estudo"):
        DicomReader().read(tmp_path)
