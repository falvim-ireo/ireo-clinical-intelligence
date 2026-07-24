"""Cobertura da inteligência estrutural DICOM da Sprint 13."""

from __future__ import annotations

import json
from pathlib import Path

from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

import radiology.dicom_reader as dicom_module
from radiology.dicom_reader import DicomReader


def write_dicom(
    path: Path,
    *,
    study_uid: str | None = None,
    series_uid: str | None = None,
    patient_name: str | None = "Antônio^Prado",
    patient_id: str | None = "123",
    modality: str | None = "CT",
    description: str | None = "CBCT odontológica",
    instance_number: int | None = 1,
    complete_geometry: bool = False,
) -> Path:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    dataset = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    dataset.SOPClassUID = CTImageStorage
    dataset.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    if patient_name is not None:
        dataset.PatientName = patient_name
    if patient_id is not None:
        dataset.PatientID = patient_id
    if study_uid is not None:
        dataset.StudyInstanceUID = study_uid
    if series_uid is not None:
        dataset.SeriesInstanceUID = series_uid
    if modality is not None:
        dataset.Modality = modality
    if description is not None:
        dataset.StudyDescription = description
        dataset.SeriesDescription = description
    if instance_number is not None:
        dataset.InstanceNumber = instance_number
    dataset.PatientBirthDate = "19800102"
    dataset.PatientSex = "M"
    dataset.StudyDate = "20260721"
    dataset.StudyTime = "101112"
    dataset.Manufacturer = "Fabricante"
    dataset.ManufacturerModelName = "Modelo"
    dataset.SoftwareVersions = "1.2.3"
    if complete_geometry:
        dataset.Rows = 400
        dataset.Columns = 500
        dataset.PixelSpacing = ["0.2", "0.3"]
        dataset.SliceThickness = "0.4"
        dataset.SpacingBetweenSlices = "0.5"
        dataset.ImagePositionPatient = ["0", "0", str((instance_number or 1) * 0.5)]
        dataset.ImageOrientationPatient = ["1", "0", "0", "0", "1", "0"]
        dataset.KVP = "90"
        dataset.Exposure = "25"
    path.parent.mkdir(parents=True, exist_ok=True)
    dataset.save_as(path, enforce_file_format=True)
    return path


def test_inventories_dicom_with_and_without_extension_and_other_files(tmp_path: Path) -> None:
    study_uid = generate_uid()
    series_uid = generate_uid()
    write_dicom(tmp_path / "with.dcm", study_uid=study_uid, series_uid=series_uid)
    write_dicom(tmp_path / "without-extension", study_uid=study_uid, series_uid=series_uid)
    (tmp_path / "corrupted.dcm").write_bytes(b"corrupted")
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.7")
    (tmp_path / "photo.jpg").write_bytes(b"image")
    (tmp_path / "viewer.exe").write_bytes(b"MZ")
    (tmp_path / "notes.txt").write_text("auxiliar", encoding="utf-8")

    analysis = DicomReader().analyze(tmp_path)

    assert analysis.valid_dicom_count == 2
    assert {item.path for item in analysis.valid_dicoms} == {"with.dcm", "without-extension"}
    assert analysis.invalid_dicoms == ()
    assert analysis.unrecognized_files == ("corrupted.dcm",)
    assert analysis.pdfs == ("report.pdf",)
    assert analysis.conventional_images == ("photo.jpg",)
    assert analysis.executable_viewers == ("viewer.exe",)
    assert analysis.auxiliary_files == ()
    assert analysis.non_dicom_files == ("notes.txt",)
    assert not any("DICOM corrompidos" in alert for alert in analysis.alerts)


def test_separates_true_corruption_non_dicom_unrecognized_and_proprietary(
    tmp_path: Path,
) -> None:
    (tmp_path / "broken").write_bytes(b"\0" * 128 + b"DICM" + b"invalid")
    (tmp_path / "not-dicom.txt").write_text("texto comum", encoding="utf-8")
    (tmp_path / "unknown.dcm").write_bytes(b"bytes arbitrarios")
    (tmp_path / "volume.SL").write_bytes(b"formato proprietario")

    analysis = DicomReader().analyze(tmp_path)

    assert analysis.invalid_dicoms == ("broken",)
    assert analysis.non_dicom_files == ("not-dicom.txt",)
    assert analysis.unrecognized_files == ("unknown.dcm",)
    assert analysis.proprietary_files == ("volume.SL",)
    assert "DICOM corrompidos: 1" in analysis.alerts


def test_groups_multiple_series_and_studies(tmp_path: Path) -> None:
    first_study = generate_uid()
    second_study = generate_uid()
    first_series = generate_uid()
    second_series = generate_uid()
    write_dicom(tmp_path / "a.dcm", study_uid=first_study, series_uid=first_series)
    write_dicom(tmp_path / "b.dcm", study_uid=first_study, series_uid=second_series)
    write_dicom(tmp_path / "c.dcm", study_uid=second_study, series_uid=generate_uid())

    analysis = DicomReader().analyze(tmp_path)

    assert analysis.study_count == 2
    assert analysis.series_count == 3
    assert "múltiplos estudos" in analysis.alerts


def test_multiple_patients_requires_manual_review(tmp_path: Path) -> None:
    study_uid = generate_uid()
    series_uid = generate_uid()
    write_dicom(
        tmp_path / "one.dcm",
        study_uid=study_uid,
        series_uid=series_uid,
        patient_name="Paciente^Um",
        patient_id="1",
    )
    write_dicom(
        tmp_path / "two.dcm",
        study_uid=study_uid,
        series_uid=series_uid,
        patient_name="Paciente^Dois",
        patient_id="2",
    )

    analysis = DicomReader().analyze(tmp_path)

    assert analysis.patient_count == 2
    assert analysis.requires_manual_review is True
    assert "múltiplos pacientes" in analysis.alerts


def test_absence_of_dicom_and_incomplete_series_generate_alerts(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "notes.txt").write_text("auxiliar", encoding="utf-8")
    assert "ausência de DICOM" in DicomReader().analyze(empty).alerts

    study_uid = generate_uid()
    series_uid = generate_uid()
    write_dicom(
        tmp_path / "one.dcm",
        study_uid=study_uid,
        series_uid=series_uid,
        instance_number=1,
    )
    write_dicom(
        tmp_path / "three.dcm",
        study_uid=study_uid,
        series_uid=series_uid,
        instance_number=3,
    )

    analysis = DicomReader().analyze(tmp_path)

    assert any("série possivelmente incompleta" in alert for alert in analysis.alerts)


def test_missing_tags_and_duplicate_uids_generate_alerts(tmp_path: Path) -> None:
    first = write_dicom(tmp_path / "missing.dcm", study_uid=None, series_uid=None)
    second = write_dicom(tmp_path / "duplicate.dcm", study_uid=None, series_uid=None)
    # Regrava o segundo com o mesmo SOPInstanceUID do primeiro.
    first_dataset = dicom_module.dcmread(first, stop_before_pixels=True)
    second_dataset = dicom_module.dcmread(second, stop_before_pixels=True)
    second_dataset.SOPInstanceUID = first_dataset.SOPInstanceUID
    second_dataset.save_as(second, enforce_file_format=True)

    analysis = DicomReader().analyze(tmp_path)

    assert analysis.valid_dicom_count == 2
    assert any(alert.startswith("metadados ausentes:") for alert in analysis.alerts)
    assert "UIDs duplicados" in analysis.alerts


def test_accent_normalization_is_comparison_only(tmp_path: Path) -> None:
    write_dicom(
        tmp_path / "accent.dcm",
        study_uid=generate_uid(),
        series_uid=generate_uid(),
        patient_name="Antônio^Custódio de Souza Prado",
        patient_id="123",
    )

    analysis = DicomReader().analyze(
        tmp_path,
        confirmed_patient_name="Antonio Custodio de Souza Prado",
        confirmed_patient_id="123",
    )

    assert not any("nome incompatível" in alert for alert in analysis.alerts)
    assert analysis.valid_dicoms[0].patient_name == "Antônio^Custódio de Souza Prado"


def test_incompatible_confirmed_identity_generates_alerts(tmp_path: Path) -> None:
    write_dicom(
        tmp_path / "identity.dcm",
        study_uid=generate_uid(),
        series_uid=generate_uid(),
        patient_name="Paciente^DICOM",
        patient_id="DICOM-1",
    )

    analysis = DicomReader().analyze(
        tmp_path,
        confirmed_patient_name="Paciente Clinicorp",
        confirmed_patient_id="CLINICORP-2",
    )

    assert "nome incompatível com o paciente confirmado" in analysis.alerts
    assert "PatientID incompatível com o paciente confirmado" in analysis.alerts


def test_estimated_voxel_and_fov_record_sources(tmp_path: Path) -> None:
    study_uid = generate_uid()
    series_uid = generate_uid()
    write_dicom(
        tmp_path / "one.dcm",
        study_uid=study_uid,
        series_uid=series_uid,
        instance_number=1,
        complete_geometry=True,
    )
    write_dicom(
        tmp_path / "two.dcm",
        study_uid=study_uid,
        series_uid=series_uid,
        instance_number=2,
        complete_geometry=True,
    )

    series = DicomReader().analyze(tmp_path).studies[0].series[0]

    assert series.estimated_voxel_size is not None
    assert series.estimated_voxel_size.estimated is True
    assert series.estimated_voxel_size.value == (0.2, 0.3, 0.5)
    assert series.estimated_voxel_size.sources == ("PixelSpacing", "SpacingBetweenSlices")
    assert series.estimated_fov is not None
    assert series.estimated_fov.value == (80.0, 150.0, 1.0)
    assert "ImagePositionPatient" in series.estimated_fov.sources
    assert series.kvp == 90.0
    assert series.exposure == {"Exposure": 25.0}


def test_classification_and_reports_are_structural(tmp_path: Path) -> None:
    write_dicom(
        tmp_path / "cbct.dcm",
        study_uid=generate_uid(),
        series_uid=generate_uid(),
        description="Cone Beam Dental",
    )
    analysis = DicomReader().analyze(tmp_path)

    summary, text = DicomReader().write_reports(analysis, tmp_path)

    payload = json.loads(summary.read_text(encoding="utf-8"))
    assert analysis.probable_classification == "CBCT odontológica"
    assert payload["probable_classification"] == "CBCT odontológica"
    assert payload["valid_dicom_count"] == 1
    report = text.read_text(encoding="utf-8")
    assert "sem interpretação clínica" in report
    assert "DICOM válidos: 1" in report


def test_reader_always_stops_before_pixels(tmp_path: Path, monkeypatch) -> None:
    path = write_dicom(
        tmp_path / "pixel.dcm",
        study_uid=generate_uid(),
        series_uid=generate_uid(),
    )
    original = dicom_module.dcmread
    calls: list[bool] = []

    def tracking_read(target, **kwargs):
        calls.append(kwargs.get("stop_before_pixels"))
        return original(target, **kwargs)

    monkeypatch.setattr(dicom_module, "dcmread", tracking_read)

    DicomReader().analyze(path.parent)

    assert calls == [True]
