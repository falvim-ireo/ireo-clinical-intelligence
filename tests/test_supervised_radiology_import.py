"""Testes totalmente offline do MVP supervisionado de radiologia."""

from datetime import date, datetime, timezone
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import subprocess
from types import SimpleNamespace
import zipfile

import pytest
import requests
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

from integrations.onedrive_graph import (
    GraphFolder,
    GraphUploadedItem,
    OneDriveGraphError,
)
from models.email_message import EmailMessage
from models.patient import Patient
from models.resolved_patient import ResolvedPatient, ResolutionReason
from observability.audit_logger import AuditLogger, mask_patient_id
from radiology.archive_extractor import (
    ArchiveExtractionError,
    ArchiveExtractor,
)
from radiology.supervised_import import (
    ConfirmedPatient,
    SupervisedImportCancelled,
    SupervisedImportError,
    SupervisedRadiologyImporter,
)


def test_transfernow_viewer_package_keeps_relative_structure_under_tomography(tmp_path):
    extracted = tmp_path / "extracted"
    (extracted / "DICOM").mkdir(parents=True)
    (extracted / "bin").mkdir()
    (extracted / "viewer.exe").write_bytes(b"MZ")
    (extracted / "DICOM" / "image").write_bytes(
        b"\x00" * 128 + b"DICM" + b"dataset"
    )
    (extracted / "bin" / "series.dat").write_bytes(b"relative dependency")

    SupervisedRadiologyImporter._preserve_structured_tomography(
        extracted, "transfernow"
    )

    package = extracted / "03 - Tomografia" / "Pacote Original"
    assert (package / "viewer.exe").is_file()
    assert (package / "DICOM" / "image").is_file()
    assert (package / "bin" / "series.dat").is_file()
from radiology.intake_history import IntakeHistoryError, IntakeHistoryRepository
from radiology.exam_index_service import ExamIndexService
from repositories.patient_repository import InMemoryPatientRepository
from repositories.patient_repository import EmptyPatientRepository
from repositories.patient_repository import PatientRepositoryUnavailableError


PATIENT_NAME = "CLÁUDIA EXEMPLO FICTÍCIA"


class FakeOneDriveClient:
    def __init__(self) -> None:
        self.root = GraphFolder("root", "Pacientes", drive_id="drive")
        self.folders: dict[tuple[str, str], GraphFolder] = {}
        self.uploads: list[tuple[str, str, bytes]] = []
        self.children: dict[str, list[dict[str, object]]] = {"root": []}
        self.contents: dict[tuple[str, str], bytes] = {}
        self.fail_once_names: set[str] = set()
        self.failed_names: set[str] = set()
        self.counter = 0

    def find_root_folder(self, configured_root: str) -> GraphFolder:
        assert configured_root == "Pacientes"
        return self.root

    def find_child_folder(self, parent: GraphFolder, name: str) -> GraphFolder | None:
        return self.folders.get((parent.item_id, name.casefold()))

    def create_folder(self, parent: GraphFolder, name: str) -> GraphFolder:
        self.counter += 1
        folder = GraphFolder(f"folder-{self.counter}", name, drive_id="drive")
        self.folders[(parent.item_id, name.casefold())] = folder
        self.children.setdefault(parent.item_id, []).append(
            {
                "id": folder.item_id,
                "name": name,
                "folder": {},
                "parentReference": {"driveId": "drive"},
            }
        )
        self.children.setdefault(folder.item_id, [])
        return folder

    def ensure_folder(self, parent: GraphFolder, name: str) -> GraphFolder:
        return self.find_child_folder(parent, name) or self.create_folder(parent, name)

    def list_children(self, folder: GraphFolder) -> list[dict[str, object]]:
        return list(self.children.get(folder.item_id, []))

    def upload_small_file(
        self, folder: GraphFolder, local_file: str | Path, remote_filename: str | None = None,
        *, progress_callback=None, retry_callback=None,
    ) -> GraphUploadedItem:
        path = Path(local_file)
        name = remote_filename or path.name
        if name in self.fail_once_names and name not in self.failed_names:
            self.failed_names.add(name)
            raise OneDriveGraphError("falha transitória simulada")
        content = path.read_bytes()
        self.uploads.append((folder.item_id, name, content))
        self.contents[(folder.item_id, name.casefold())] = content
        children = self.children.setdefault(folder.item_id, [])
        children[:] = [item for item in children if str(item.get("name", "")).casefold() != name.casefold()]
        children.append(
            {"id": f"file-{len(self.uploads)}", "name": name, "size": len(content), "file": {}}
        )
        if progress_callback is not None:
            progress_callback(len(content), len(content))
        return GraphUploadedItem(name=name, size=path.stat().st_size, has_id=True)

    def download_json_file(self, folder: GraphFolder, filename: str):
        content = self.contents.get((folder.item_id, filename.casefold()))
        return json.loads(content) if content is not None else None


@pytest.fixture(autouse=True)
def forbid_external_actions(monkeypatch) -> None:
    def forbidden_network(*args, **kwargs):
        raise AssertionError("Acesso de rede proibido no teste supervisionado")

    monkeypatch.setattr(requests, "get", forbidden_network)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden_network)
    monkeypatch.setattr(socket, "create_connection", forbidden_network)


def create_zip(path: Path, files: dict[str, bytes] | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in (files or {"scan/image.dcm": b"dicom-ficticio"}).items():
            archive.writestr(name, content)
    return path


def create_dicom_zip(
    path: Path,
    tmp_path: Path,
    *,
    patient_names: tuple[str, ...] = (PATIENT_NAME,),
    study_date: str = "20991231",
    study_time: str = "235900",
) -> Path:
    study_uid = generate_uid()
    series_uid = generate_uid()
    sources = []
    for index, patient_name in enumerate(patient_names, start=1):
        source = tmp_path / f"dicom-source-{index}"
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = CTImageStorage
        meta.MediaStorageSOPInstanceUID = generate_uid()
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        dataset = FileDataset(str(source), {}, file_meta=meta, preamble=b"\0" * 128)
        dataset.SOPClassUID = CTImageStorage
        dataset.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
        dataset.PatientName = patient_name.replace(" ", "^")
        dataset.PatientID = str(990000010 + index - 1)
        dataset.StudyInstanceUID = study_uid
        dataset.SeriesInstanceUID = series_uid
        dataset.Modality = "CT"
        dataset.StudyDate = study_date
        dataset.StudyTime = study_time
        dataset.StudyDescription = "CBCT odontológica"
        dataset.SeriesDescription = "Cone Beam"
        dataset.Rows = 100
        dataset.Columns = 120
        dataset.PixelSpacing = ["0.3", "0.3"]
        dataset.SliceThickness = "0.4"
        dataset.save_as(source, enforce_file_format=True)
        sources.append(source)
    with zipfile.ZipFile(path, "w") as archive:
        for index, source in enumerate(sources, start=1):
            archive.write(source, f"DICOM/image-{index}")
    return path


def input_sequence(*answers: str):
    values = iter(answers)
    return lambda prompt: next(values)


def build_importer(
    tmp_path: Path,
    *,
    answers: tuple[str, ...] = ("1", "1", "CONFIRMAR"),
    patients: list[Patient] | None = None,
    patient_folders: tuple[str, ...] = (PATIENT_NAME,),
    gmail_connector=None,
    downloader=None,
    auto_select_unambiguous: bool = False,
    auto_select_min_score: float = 0.95,
    force_manual_selection: bool = False,
    patient_source_mode: str = "clinicorp",
    audit_logger=None,
    intake_history=None,
    allow_reimport: bool = False,
) -> tuple[SupervisedRadiologyImporter, Path, Path, list[str]]:
    quarantine = tmp_path / "quarantine"
    patients_root = quarantine / "supervised-staging"
    tool = tmp_path / "UnRAR.exe"
    tool.write_bytes(b"ferramenta-ficticia")
    output: list[str] = []
    audit = audit_logger or AuditLogger(correlation_id="correlation-supervised-0001")
    importer = SupervisedRadiologyImporter(
        quarantine_root=quarantine,
        archive_tool_path=tool,
        onedrive_client=FakeOneDriveClient(),
        onedrive_root="Pacientes",
        patient_repository=InMemoryPatientRepository(
            patients
            if patients is not None
            else [Patient(id=990000010, nome=PATIENT_NAME)]
        ),
        gmail_connector=gmail_connector,
        audit_logger=audit,
        input_func=input_sequence(*answers),
        output=output.append,
        downloader=downloader,
        today_provider=lambda: date(2099, 12, 31),
        now_provider=lambda: datetime(
            2099,
            12,
            31,
            23,
            59,
            tzinfo=timezone.utc,
        ),
        auto_select_unambiguous=auto_select_unambiguous,
        auto_select_min_score=auto_select_min_score,
        force_manual_selection=force_manual_selection,
        patient_source_mode=patient_source_mode,
        intake_history=intake_history,
        allow_reimport=allow_reimport,
    )
    return importer, patients_root, quarantine, output


def test_local_zip_runs_complete_supervised_copy_and_manifest(tmp_path: Path) -> None:
    archive = create_zip(
        tmp_path / f"{PATIENT_NAME}_20991231.zip",
        {
            "scan/image-1.dcm": b"primeiro-conteudo-ficticio",
            "report.txt": b"laudo-ficticio",
        },
    )
    importer, _, _, output = build_importer(tmp_path)

    result = importer.run(archive_path=archive)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert result.destination.name == "2099-12-31 - Radiologia"
    assert result.file_count == 2
    assert result.total_size_bytes == sum(
        len(value)
        for value in (
            b"primeiro-conteudo-ficticio",
            b"laudo-ficticio",
        )
    )
    assert archive.exists()
    assert (result.destination / "scan" / "image-1.dcm").is_file()
    assert manifest["correlation_id"] == "correlation-supervised-0001"
    assert manifest["timestamp_utc"] == "2099-12-31T23:59:00Z"
    assert manifest["original_archive"] == archive.name
    assert manifest["masked_patient_id"] == mask_patient_id(990000010)
    assert manifest["source"] == "local"
    assert manifest["status"] == "COMPLETED"
    assert manifest["destination"] == str(result.destination)
    assert manifest["onedrive_destination"] == result.onedrive_destination
    assert result.onedrive_destination.endswith(
        f"/{PATIENT_NAME}/Radiologia/2099-12-31 - Radiologia"
    )
    uploaded_names = [name for _, name, _ in importer.onedrive_client.uploads]
    assert uploaded_names.count("image-1.dcm") == 1
    assert uploaded_names.count("report.txt") == 1
    assert uploaded_names.count("manifest.json") >= 2
    assert any(line.startswith("Arquivo de origem:") for line in output)
    assert "Quantidade de arquivos: 2" in output
    assert any(
        "Coincidências locais por nome e tamanho (apenas diagnóstico): 0" in line
        for line in output
    )


def test_extracts_clinical_date_and_time_from_compound_archive_name() -> None:
    exam_date, exam_time = SupervisedRadiologyImporter._date_time_from_archive_name(
        "ANTONIO CUSTODIO DE SOUZA PRADO_20260310111013.SL.rar"
    )
    assert exam_date == date(2026, 3, 10)
    assert exam_time is not None and exam_time.isoformat() == "11:10:13"


def test_archive_date_drives_destination_and_manifest(tmp_path: Path) -> None:
    archive = create_zip(
        tmp_path / f"{PATIENT_NAME}_20260310111013.zip",
        {"viewer/data.bin": b"proprietary"},
    )
    importer, _, _, _ = build_importer(tmp_path)

    result = importer.run(archive_path=archive)
    manifest = json.loads(result.manifest_path.read_text("utf-8"))

    assert result.destination.name == "2026-03-10 - Radiologia"
    assert manifest["exam_date"] == "2026-03-10"
    assert manifest["exam_time"] == "11:10:13"
    assert manifest["exam_date_source"] == "ARCHIVE_FILENAME"
    assert manifest["import_started_at"]
    assert manifest["import_completed_at"]


def test_consistent_dicom_study_date_precedes_archive_name(tmp_path: Path) -> None:
    archive = create_dicom_zip(
        tmp_path / f"{PATIENT_NAME}_20260310111013.zip",
        tmp_path,
        study_date="20260201",
        study_time="081500",
    )
    importer, _, _, _ = build_importer(tmp_path)

    result = importer.run(archive_path=archive)
    manifest = json.loads(result.manifest_path.read_text("utf-8"))

    assert result.destination.name == "2026-02-01 - Radiologia"
    assert manifest["exam_date_source"] == "DICOM_STUDY_DATE"
    assert manifest["exam_time"] == "08:15:00"


def test_different_clinical_dates_imported_on_same_day_use_distinct_destinations(
    tmp_path: Path,
) -> None:
    first_root, second_root = tmp_path / "first", tmp_path / "second"
    first_root.mkdir(); second_root.mkdir()
    first_archive = create_zip(
        first_root / f"{PATIENT_NAME}_20260310111013.zip", {"a.bin": b"first"}
    )
    second_archive = create_zip(
        second_root / f"{PATIENT_NAME}_20260411121013.zip", {"b.bin": b"second"}
    )
    first, _, _, _ = build_importer(first_root)
    second, _, _, _ = build_importer(second_root)
    second.onedrive_client = first.onedrive_client

    first_result = first.run(archive_path=first_archive)
    second_result = second.run(archive_path=second_archive)

    assert first_result.onedrive_destination.endswith("/2026-03-10 - Radiologia")
    assert second_result.onedrive_destination.endswith("/2026-04-11 - Radiologia")


def test_two_different_exams_same_patient_and_day_are_distinguished_by_time(
    tmp_path: Path,
) -> None:
    first_root, second_root = tmp_path / "first", tmp_path / "second"
    first_root.mkdir(); second_root.mkdir()
    first_archive = create_zip(
        first_root / f"{PATIENT_NAME}_20260310111013.zip", {"scan.bin": b"first"}
    )
    second_archive = create_zip(
        second_root / f"{PATIENT_NAME}_20260310124559.zip", {"scan.bin": b"second"}
    )
    first, _, _, _ = build_importer(first_root)
    second, _, _, _ = build_importer(second_root)
    second.onedrive_client = first.onedrive_client

    first_result = first.run(archive_path=first_archive)
    second_result = second.run(archive_path=second_archive)

    assert first_result.onedrive_destination.endswith("/2026-03-10 - Radiologia")
    assert second_result.onedrive_destination.endswith(
        "/2026-03-10 12-45 - Radiologia"
    )
    assert "(2)" not in second_result.onedrive_destination


def test_missing_clinical_date_uses_explicit_import_date_fallback(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}.zip", {"scan.bin": b"unknown"})
    importer, _, _, _ = build_importer(tmp_path)

    result = importer.run(archive_path=archive)
    manifest = json.loads(result.manifest_path.read_text("utf-8"))

    assert result.destination.name == "2099-12-31 - Radiologia"
    assert manifest["exam_date_source"] == "IMPORT_DATE_FALLBACK"


def test_checksums_match_every_copied_file(tmp_path: Path) -> None:
    content = b"checksum-ficticio"
    archive = create_zip(
        tmp_path / f"{PATIENT_NAME}_20991231.zip",
        {"nested/scan.bin": content},
    )
    importer, _, _, _ = build_importer(tmp_path)

    result = importer.run(archive_path=archive)

    expected = hashlib.sha256(content).hexdigest()
    assert result.checksums == {"nested/scan.bin": expected}
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["checksums"] == {"nested/scan.bin": expected}


def test_dicom_intelligence_reports_manifest_and_onedrive_publication(
    tmp_path: Path,
) -> None:
    archive = create_dicom_zip(
        tmp_path / f"{PATIENT_NAME}_20991231.zip", tmp_path
    )
    importer, _, _, output = build_importer(tmp_path)

    result = importer.run(archive_path=archive)

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    intelligence = manifest["dicom_intelligence"]
    assert result.file_count == 1
    assert intelligence["valid_dicom_count"] == 1
    assert intelligence["study_count"] == 1
    assert intelligence["series_count"] == 1
    assert intelligence["patient_count"] == 1
    assert intelligence["probable_classification"] == "CBCT odontológica"
    assert (result.destination / "dicom_summary.json").is_file()
    assert (result.destination / "resumo_do_exame.txt").is_file()
    uploaded_names = [name for _, name, _ in importer.onedrive_client.uploads]
    assert "dicom_summary.json" in uploaded_names
    assert "resumo_do_exame.txt" in uploaded_names
    assert any("DICOM válidos: 1" in line for line in output)
    assert any("Estudos: 1" in line for line in output)
    assert any("Séries: 1" in line for line in output)
    assert any("Classificação provável: CBCT odontológica" in line for line in output)


def test_completed_publication_is_incrementally_indexed(tmp_path: Path) -> None:
    archive = create_dicom_zip(
        tmp_path / f"{PATIENT_NAME}_20260310111013.zip", tmp_path,
        study_date="20260310", study_time="111013",
    )
    importer, _, _, output = build_importer(tmp_path)
    index = ExamIndexService(tmp_path / "radiology-index.db")
    importer.exam_index_service = index

    result = importer.run(archive_path=archive)
    exam_id = json.loads(result.manifest_path.read_text("utf-8"))["publication"]["exam_id"]

    indexed = index.get_by_exam_id(exam_id)
    assert indexed is not None
    assert indexed.exam_date == "2026-03-10"
    assert indexed.modality == "CT"
    assert "Indexação............. OK" in output


def test_cfaz_metadata_uses_existing_pipeline_manifest_index_and_dashboard(
    tmp_path: Path,
) -> None:
    archive = create_zip(
        tmp_path / f"{PATIENT_NAME}_20260310111013_CFAZ-307471.zip",
        {
            "panoramica.jpg": b"pan",
            "telerradiografia.png": b"tele",
            "laudo.pdf": b"report",
        },
    )
    importer, _, _, _ = build_importer(tmp_path)
    index = ExamIndexService(tmp_path / "cfaz-index.db")
    importer.exam_index_service = index
    metadata = {
        "provider_id": "cfaz",
        "request_id": "307471",
        "provider_exam_id": "reports:99",
        "request_date": "2026-03-10T11:00:00-03:00",
        "exam_date": "2026-03-10T11:10:13-03:00",
        "patient_name": PATIENT_NAME,
        "radiology_clinic": "Radiologia Exemplo",
        "professional": "Dra. Solicitante",
        "source_url": "https://max.cfaz.net/requests/307471",
        "classifications": ["Laudo", "Panorâmica", "Telerradiografia"],
        "asset_count": 3,
    }

    result = importer.run(
        archive_path=archive,
        archive_sha256="b" * 64,
        acquisition_metadata=metadata,
        acquisition_exam_id="a" * 64,
        source_provider="cfaz",
        sender_exam_date="2026-03-10",
    )
    stored = json.loads(result.manifest_path.read_text("utf-8"))

    assert result.destination.name == "2026-03-10 - Documentação Radiológica"
    assert stored["source"] == "cfaz"
    assert stored["acquisition"] == metadata
    assert stored["publication"]["exam_id"] == "a" * 64
    assert stored["publication"]["source_archive_sha256"] == "b" * 64
    indexed = index.get_by_exam_id("a" * 64)
    assert indexed is not None
    assert indexed.modality == "Laudo,Panorâmica,Telerradiografia"
    dashboard = index.dashboard()
    assert dashboard["exams"] == 1
    assert dashboard["modalities"][0]["modality"] == indexed.modality


def test_multiple_dicom_patients_block_publication_before_onedrive(
    tmp_path: Path,
) -> None:
    archive = create_dicom_zip(
        tmp_path / f"{PATIENT_NAME}_20991231.zip",
        tmp_path,
        patient_names=(PATIENT_NAME, "OUTRO PACIENTE"),
    )
    importer, _, _, output = build_importer(tmp_path, answers=("1",))

    with pytest.raises(SupervisedImportError, match="múltiplos pacientes DICOM"):
        importer.run(archive_path=archive)

    assert importer.onedrive_client.list_children(importer.onedrive_client.root) == []
    assert any("Pacientes encontrados: 2" in line for line in output)
    assert any("múltiplos pacientes" in line for line in output)


def test_local_rar_uses_unrar_without_shell_and_keeps_inputs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    archive = tmp_path / f"{PATIENT_NAME}_20991231.rar"
    archive.write_bytes(b"rar-ficticio")
    importer, _, _, _ = build_importer(tmp_path)
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[1] == "lb":
            listing = "scan/image.dcm\n"
            return subprocess.CompletedProcess(command, 0, listing, "")
        destination = Path(command[-1].rstrip("\\/"))
        (destination / "scan").mkdir(parents=True, exist_ok=True)
        (destination / "scan" / "image.dcm").write_bytes(b"rar-extraido")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = importer.run(archive_path=archive)

    assert archive.exists()
    assert result.file_count == 1
    assert len(calls) == 2
    assert calls[0][0][0].endswith("UnRAR.exe")
    assert calls[0][0][1:] == ["lb", str(archive.resolve())]
    assert calls[1][0][0].endswith("UnRAR.exe")
    assert calls[1][0][1:3] == ["x", "-o-"]
    assert calls[1][0][-2] == str(archive.resolve())
    assert calls[1][0][-1].endswith(os.sep)
    assert all("WinRAR.exe" not in argument for call, _ in calls for argument in call)
    assert all(call_kwargs["shell"] is False for _, call_kwargs in calls)
    assert all(call_kwargs["timeout"] == 1800 for _, call_kwargs in calls)
    assert all(call_kwargs["capture_output"] is True for _, call_kwargs in calls)
    assert all(
        call_kwargs["creationflags"]
        == getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for _, call_kwargs in calls
    )


def test_unrar_not_found_is_safe(tmp_path: Path) -> None:
    archive = tmp_path / "PACIENTE FICTICIO.rar"
    archive.write_bytes(b"rar")
    extractor = ArchiveExtractor(
        tmp_path / "quarantine",
        tmp_path / "missing-unrar.exe",
    )

    with pytest.raises(ArchiveExtractionError, match="não foi encontrado"):
        extractor.extract(archive)

    assert archive.exists()


def test_unrar_failure_is_checked(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "PACIENTE FICTICIO.rar"
    archive.write_bytes(b"rar")
    tool = tmp_path / "UnRAR.exe"
    tool.write_bytes(b"tool")
    extractor = ArchiveExtractor(tmp_path / "quarantine", tool)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 7, "", "secret"),
    )

    with pytest.raises(ArchiveExtractionError, match="não conseguiu"):
        extractor.extract(archive)


def test_unrar_timeout_is_safe(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "PACIENTE FICTICIO.rar"
    archive.write_bytes(b"rar")
    tool = tmp_path / "UnRAR.exe"
    tool.write_bytes(b"tool")
    extractor = ArchiveExtractor(tmp_path / "quarantine", tool, timeout_seconds=1)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1, output="sensitive")

    monkeypatch.setattr(subprocess, "run", timeout)

    with pytest.raises(ArchiveExtractionError, match="tempo limite"):
        extractor.extract(archive)


def test_unrar_listing_blocks_traversal_before_extraction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    archive = tmp_path / "PACIENTE FICTICIO.rar"
    archive.write_bytes(b"rar")
    tool = tmp_path / "UnRAR.exe"
    tool.write_bytes(b"tool")
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        listing = "../../outside.txt\n"
        return subprocess.CompletedProcess(command, 0, listing, "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    extractor = ArchiveExtractor(tmp_path / "quarantine", tool)

    with pytest.raises(ArchiveExtractionError, match="caminho inseguro"):
        extractor.extract(archive)

    assert len(calls) == 1
    assert calls[0][1] == "lb"
    assert not (tmp_path / "outside.txt").exists()


def test_unrar_lb_accepts_large_real_pilot_style_listing() -> None:
    listing = "\n".join(
        f"VERA LUCIA CRUZ/CT/SERIE_{index:04d}/IMAGEM_{index:04d}.dcm"
        for index in range(1200)
    )
    listing = f"\n{listing}\n\n"

    members = ArchiveExtractor._parse_unrar_listing(listing)

    assert len(members) == 1200
    assert members[0] == "VERA LUCIA CRUZ/CT/SERIE_0000/IMAGEM_0000.dcm"
    assert members[-1] == "VERA LUCIA CRUZ/CT/SERIE_1199/IMAGEM_1199.dcm"


def test_missing_local_archive_is_rejected(tmp_path: Path) -> None:
    importer, _, _, _ = build_importer(tmp_path)

    with pytest.raises(SupervisedImportError, match="não existe"):
        importer.run(archive_path=tmp_path / "missing.zip")


def test_multiple_clinicorp_candidates_require_human_selection(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    patients = [
        Patient(id=990000011, nome=PATIENT_NAME),
        Patient(id=990000012, nome=PATIENT_NAME),
    ]
    importer, _, _, output = build_importer(
        tmp_path,
        answers=("2", "1", "CONFIRMAR"),
        patients=patients,
    )

    result = importer.run(archive_path=archive)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert manifest["masked_patient_id"] == mask_patient_id(990000012)
    assert any("990000011" in line for line in output)
    assert any("990000012" in line for line in output)


def test_offline_source_requires_manual_patient_confirmation(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, _, _, _ = build_importer(
        tmp_path,
        answers=("CONFIRMAR", "1", "CONFIRMAR"),
    )
    importer.patient_repository = EmptyPatientRepository()

    result = importer.run(archive_path=archive)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert manifest["masked_patient_id"] is None
    assert result.destination.is_dir()


def test_remote_destination_uses_confirmed_patient_name(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    folders = (
        f"001 - {PATIENT_NAME}",
        f"002 - {PATIENT_NAME}",
    )
    importer, patients_root, _, output = build_importer(
        tmp_path,
        answers=("1", "1", "CONFIRMAR"),
        patient_folders=folders,
    )

    result = importer.run(archive_path=archive)

    assert result.destination.is_relative_to(patients_root / PATIENT_NAME)
    assert any("Pacientes/" + PATIENT_NAME + "/Radiologia" in line for line in output)
    assert any(name == "manifest.json" for _, name, _ in importer.onedrive_client.uploads)


def test_missing_remote_patient_folder_is_created_via_graph(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, patients_root, _, _ = build_importer(
        tmp_path,
        answers=("1", "1", "CONFIRMAR"),
        patient_folders=(),
    )

    result = importer.run(archive_path=archive)

    assert result.destination.is_relative_to(patients_root)
    assert ("root", PATIENT_NAME.casefold()) in importer.onedrive_client.folders


def test_existing_remote_destination_is_never_overwritten(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, _, _, _ = build_importer(tmp_path)
    client = importer.onedrive_client
    patient = client.ensure_folder(client.root, PATIENT_NAME)
    radiology = client.ensure_folder(patient, "Radiologia")
    client.create_folder(radiology, "2099-12-31 - Radiologia")

    with pytest.raises(SupervisedImportError, match="sem manifesto de estado"):
        importer.run(archive_path=archive)

    assert client.uploads == []


def seed_remote_manifest(
    tmp_path: Path,
    client: FakeOneDriveClient,
    destination: GraphFolder,
    *,
    archive_sha256: str,
    state: str,
    uploaded_files: dict[str, dict[str, object]] | None = None,
) -> None:
    path = tmp_path / f"manifest-{destination.item_id}.json"
    path.write_text(
        json.dumps(
            {
                "publication": {
                    "state": state,
                    "exam_id": archive_sha256,
                    "source_archive_sha256": archive_sha256,
                    "total_files": len(uploaded_files or {}),
                    "total_bytes": sum(
                        int(item["size"]) for item in (uploaded_files or {}).values()
                    ),
                    "uploaded_files_count": len(uploaded_files or {}),
                    "uploaded_bytes": sum(
                        int(item["size"]) for item in (uploaded_files or {}).values()
                    ),
                    "uploaded_files": uploaded_files or {},
                }
            }
        ),
        encoding="utf-8",
    )
    client.upload_small_file(destination, path, remote_filename="manifest.json")


def test_normalized_patient_name_reuses_original_onedrive_name(tmp_path: Path) -> None:
    remote_name = "Antônio  Custódio de Souza Prado"
    requested_name = "antonio custodio de souza prado"
    archive = create_zip(tmp_path / f"{requested_name}_20991231.zip")
    importer, _, _, _ = build_importer(
        tmp_path,
        patients=[Patient(id=990000010, nome=requested_name)],
    )
    client = importer.onedrive_client
    original = client.create_folder(client.root, remote_name)

    result = importer.run(archive_path=archive)

    patient_children = [
        item for item in client.list_children(client.root) if isinstance(item.get("folder"), dict)
    ]
    assert len(patient_children) == 1
    assert patient_children[0]["id"] == original.item_id
    assert patient_children[0]["name"] == remote_name
    assert f"/{remote_name}/Radiologia/" in result.onedrive_destination


def test_complete_remote_import_blocks_reimport_without_new_folder(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, _, _, _ = build_importer(tmp_path)
    client = importer.onedrive_client
    patient = client.create_folder(client.root, PATIENT_NAME)
    radiology = client.create_folder(patient, "Radiologia")
    destination = client.create_folder(radiology, "2099-12-31 - Radiologia")
    seed_remote_manifest(
        tmp_path,
        client,
        destination,
        archive_sha256=importer._file_sha256(archive),
        state="COMPLETE",
    )
    client.uploads.clear()

    with pytest.raises(SupervisedImportError, match="já está COMPLETE"):
        importer.run(archive_path=archive)

    assert client.uploads == []
    assert [item["name"] for item in client.list_children(radiology)] == [
        "2099-12-31 - Radiologia"
    ]


def test_failed_remote_import_resumes_only_missing_files(tmp_path: Path) -> None:
    archive = create_zip(
        tmp_path / f"{PATIENT_NAME}_20991231.zip",
        {"already.bin": b"already", "missing.bin": b"missing"},
    )
    importer, _, _, _ = build_importer(tmp_path)
    client = importer.onedrive_client
    patient = client.create_folder(client.root, PATIENT_NAME)
    radiology = client.create_folder(patient, "Radiologia")
    destination = client.create_folder(radiology, "2099-12-31 - Radiologia")
    already = tmp_path / "already.bin"
    already.write_bytes(b"already")
    client.upload_small_file(destination, already)
    uploaded_record = {
        "already.bin": {
            "sha256": hashlib.sha256(b"already").hexdigest(),
            "size": len(b"already"),
        }
    }
    seed_remote_manifest(
        tmp_path,
        client,
        destination,
        archive_sha256=importer._file_sha256(archive),
        state="FAILED",
        uploaded_files=uploaded_record,
    )
    client.uploads.clear()

    result = importer.run(archive_path=archive)

    data_uploads = [name for _, name, _ in client.uploads if name != "manifest.json"]
    assert "already.bin" not in data_uploads
    assert set(data_uploads) == {
        "dicom_summary.json",
        "missing.bin",
        "resumo_do_exame.txt",
    }
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["publication"]["state"] == "COMPLETE"
    assert manifest["publication"]["uploaded_files_count"] == 4


def test_multiple_legacy_destinations_are_diagnosed_without_consolidation(
    tmp_path: Path,
) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, _, _, _ = build_importer(tmp_path)
    client = importer.onedrive_client
    patient = client.create_folder(client.root, PATIENT_NAME)
    radiology = client.create_folder(patient, "Radiologia")
    states = ("IN_PROGRESS", "FAILED", "COMPLETE")
    for index, state in enumerate(states, start=1):
        suffix = "" if index == 1 else f" ({index})"
        destination = client.create_folder(
            radiology, f"2099-12-31 - Radiologia{suffix}"
        )
        seed_remote_manifest(
            tmp_path,
            client,
            destination,
            archive_sha256=importer._file_sha256(archive),
            state=state,
        )
    before = list(client.list_children(radiology))
    client.uploads.clear()

    with pytest.raises(SupervisedImportError) as captured:
        importer.run(archive_path=archive)

    message = str(captured.value)
    assert "estado=IN_PROGRESS" in message
    assert "estado=FAILED" in message
    assert "estado=COMPLETE" in message
    assert "Nenhuma pasta foi movida, mesclada ou apagada" in message
    assert client.list_children(radiology) == before
    assert client.uploads == []


def test_small_file_progress_and_final_success_are_visible(tmp_path: Path) -> None:
    archive = create_zip(
        tmp_path / f"{PATIENT_NAME}_20991231.zip",
        {"one.bin": b"1", "two.bin": b"22"},
    )
    importer, _, _, output = build_importer(tmp_path)

    importer.run(archive_path=archive)

    assert any("Upload: 1/4 arquivos" in line for line in output)
    assert any("Upload: 4/4 arquivos" in line for line in output)
    assert any("Progresso: 100.0%" in line for line in output)
    assert any("Velocidade média:" in line and "ETA:" in line for line in output)
    assert any("Upload concluído: 4/4 arquivos" in line for line in output)


def test_upload_failure_records_failed_state_and_final_line(tmp_path: Path) -> None:
    archive = create_zip(
        tmp_path / f"{PATIENT_NAME}_20991231.zip", {"fail.bin": b"failure"}
    )
    importer, _, _, output = build_importer(tmp_path)
    importer.onedrive_client.fail_once_names.add("fail.bin")

    with pytest.raises(SupervisedImportError, match="falha transitória simulada"):
        importer.run(archive_path=archive)

    manifest_path = (
        importer.staging_root
        / PATIENT_NAME
        / "Exames de imagem"
        / "2099-12-31 - Radiologia"
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["publication"]["state"] == "FAILED"
    assert manifest["status"] == "FAILED"
    assert any("Upload falhou" in line for line in output)


def test_graph_error_is_propagated_and_logged_with_stack_trace(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, _, _, _ = build_importer(tmp_path)
    original = OneDriveGraphError(
        "Mensagem completa do Microsoft Graph.",
        http_status=403,
        graph_code="accessDenied",
        graph_message="Mensagem completa do Microsoft Graph.",
        request_id="request-id-fixture",
        client_request_id="client-request-id-fixture",
        endpoint="https://graph.microsoft.com/v1.0/me/drive/root:/Pacientes",
    )

    def fail_root(configured_root: str) -> GraphFolder:
        raise original

    importer.onedrive_client.find_root_folder = fail_root

    with caplog.at_level(logging.ERROR), pytest.raises(
        SupervisedImportError, match="Mensagem completa do Microsoft Graph"
    ) as captured:
        importer.run(archive_path=archive)

    assert captured.value.__cause__ is original
    assert "HTTP status=403" in str(captured.value)
    assert "Graph code=accessDenied" in caplog.text
    assert "request-id=request-id-fixture" in caplog.text
    assert "client-request-id=client-request-id-fixture" in caplog.text
    assert "endpoint=https://graph.microsoft.com/v1.0/me/drive/root:/Pacientes" in caplog.text
    assert "Traceback (most recent call last)" in caplog.text


def test_zip_path_traversal_is_rejected_without_external_write(tmp_path: Path) -> None:
    archive = create_zip(
        tmp_path / "PACIENTE FICTICIO.zip",
        {"../../outside.txt": b"proibido"},
    )
    extractor = ArchiveExtractor(
        tmp_path / "quarantine",
        tmp_path / "UnRAR.exe",
    )

    with pytest.raises(ArchiveExtractionError, match="caminho inseguro"):
        extractor.extract(archive)

    assert not (tmp_path / "outside.txt").exists()
    assert archive.exists()


def test_staging_destination_outside_quarantine_is_rejected(tmp_path: Path) -> None:
    importer, _, _, _ = build_importer(tmp_path)
    outside = tmp_path / "outside-patient"
    outside.mkdir()

    with pytest.raises(SupervisedImportError, match="fora da área temporária"):
        importer._next_destination(outside)


def test_copy_destination_outside_root_is_rejected_before_creation(
    tmp_path: Path,
) -> None:
    importer, _, _, _ = build_importer(tmp_path)
    extracted = tmp_path / "extracted-outside-check"
    extracted.mkdir()
    source_file = extracted / "scan.bin"
    source_file.write_bytes(b"conteudo")
    outside = tmp_path / "outside" / "destination"

    with pytest.raises(SupervisedImportError, match="fora da raiz"):
        importer._copy_and_manifest(
            archive=tmp_path / "archive.zip",
            extracted=extracted,
            destination=outside,
            patient=ConfirmedPatient(PATIENT_NAME, 990000010),
            files=[source_file],
            total_size=8,
            source="local",
        )

    assert not outside.exists()


def test_existing_local_staging_is_reused_without_incremental_suffix(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, patients_root, _, _ = build_importer(tmp_path)
    existing = (
        patients_root
        / PATIENT_NAME
        / "Exames de imagem"
        / "2099-12-31 - Radiologia"
    )
    existing.mkdir(parents=True)
    marker = existing / "existing.txt"
    marker.write_text("não sobrescrever", encoding="utf-8")

    result = importer.run(archive_path=archive)

    assert result.destination.name == "2099-12-31 - Radiologia"
    assert marker.read_text(encoding="utf-8") == "não sobrescrever"


def test_preview_reports_possible_duplicate_before_copy(tmp_path: Path) -> None:
    archive = create_zip(
        tmp_path / f"{PATIENT_NAME}_20991231.zip",
        {"scan.bin": b"mesmo-tamanho"},
    )
    importer, patients_root, _, output = build_importer(tmp_path)
    previous = (
        patients_root
        / PATIENT_NAME
        / "Exames de imagem"
        / "2099-12-30 - Radiologia"
    )
    previous.mkdir(parents=True)
    (previous / "scan.bin").write_bytes(b"outro-tamanho")

    importer.run(archive_path=archive)

    assert any(
        "Coincidências locais por nome e tamanho (apenas diagnóstico): 1" in line
        for line in output
    )


def test_copy_confirmation_is_case_insensitive_and_ignores_spaces(
    tmp_path: Path,
) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, _, _, _ = build_importer(
        tmp_path,
        answers=("1", "1", "  confirmar  "),
    )

    result = importer.run(archive_path=archive)

    assert result.destination.is_dir()


def test_pipeline_finishes_without_mechanical_confirmation(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    prompts: list[str] = []
    importer, patients_root, quarantine, _ = build_importer(tmp_path)
    importer.input = lambda prompt: (prompts.append(prompt), pytest.fail(prompt))[1]

    result = importer.run(archive_path=archive)

    assert result.destination.is_relative_to(patients_root / PATIENT_NAME / "Exames de imagem")
    assert archive.exists()
    assert any(path.is_dir() for path in quarantine.iterdir())
    assert prompts == []


def test_copy_refuses_destination_created_after_preview(tmp_path: Path) -> None:
    importer, patients_root, _, _ = build_importer(tmp_path)
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    source_file = extracted / "scan.bin"
    source_file.write_bytes(b"novo")
    destination = patients_root / PATIENT_NAME / "existing-destination"
    destination.mkdir(parents=True)
    existing = destination / "scan.bin"
    existing.write_bytes(b"antigo")

    with pytest.raises(SupervisedImportError, match="nenhuma sobrescrita"):
        importer._copy_and_manifest(
            archive=tmp_path / "archive.zip",
            extracted=extracted,
            destination=destination,
            patient=ConfirmedPatient(PATIENT_NAME, 990000010),
            files=[source_file],
            total_size=4,
            source="local",
        )

    assert existing.read_bytes() == b"antigo"


def test_email_mode_uses_readonly_message_and_injected_download(tmp_path: Path) -> None:
    message = EmailMessage(
        message_id="fixture-message-id",
        subject=f'TransferNow - "{PATIENT_NAME}_20991231.zip"',
        sender="TransferNow <noreply@transfernow.net>",
        reply_to=None,
        received_at=datetime(2099, 12, 31, tzinfo=timezone.utc),
        html_body=(
            '<a href="https://transfernow.net/dl/secret-fixture-token">'
            "Baixar</a>"
        ),
    )

    class ReadOnlyGmail:
        def __init__(self) -> None:
            self.ids: list[str] = []

        def get_message(self, message_id: str) -> EmailMessage:
            self.ids.append(message_id)
            return message

    gmail = ReadOnlyGmail()
    downloads: list[tuple[str, str, Path]] = []

    def fake_download(url: str, filename: str, quarantine: Path) -> Path:
        downloads.append((url, filename, quarantine))
        quarantine.mkdir(parents=True, exist_ok=True)
        return create_zip(quarantine / filename)

    importer, _, _, _ = build_importer(
        tmp_path,
        gmail_connector=gmail,
        downloader=fake_download,
    )

    result = importer.run(email_message_id="fixture-message-id")
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

    assert gmail.ids == ["fixture-message-id"]
    assert len(downloads) == 1
    assert manifest["source"] == "gmail"
    assert manifest["email_received_at"] == "2099-12-31T00:00:00Z"
    assert "secret-fixture-token" not in result.manifest_path.read_text(
        encoding="utf-8"
    )


def test_default_downloader_failure_is_sanitized(tmp_path: Path, monkeypatch) -> None:
    importer, _, quarantine, _ = build_importer(tmp_path)

    def connection_failure(*args, **kwargs):
        raise requests.ConnectionError(
            "https://transfernow.net/dl/SECRET-TOKEN"
        )

    monkeypatch.setattr(requests, "get", connection_failure)

    with pytest.raises(SupervisedImportError) as captured:
        importer._download_transfernow(
            "https://transfernow.net/dl/SECRET-TOKEN",
            "PACIENTE FICTICIO.zip",
            quarantine,
        )

    assert "SECRET-TOKEN" not in str(captured.value)


def test_default_downloader_writes_only_inside_quarantine(
    tmp_path: Path,
    monkeypatch,
) -> None:
    importer, _, quarantine, _ = build_importer(tmp_path)
    calls = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def iter_content(self, chunk_size: int):
            assert chunk_size == 1024 * 1024
            return iter((b"parte-1", b"", b"parte-2"))

    def fake_get(url: str, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr(requests, "get", fake_get)

    downloaded = importer._download_transfernow(
        "https://transfernow.net/dl/fixture",
        "EXAME_FICTICIO.zip",
        quarantine,
    )

    assert downloaded.parent == quarantine.resolve()
    assert downloaded.read_bytes() == b"parte-1parte-2"
    assert calls == [
        (
            "https://transfernow.net/dl/fixture",
            {"stream": True, "timeout": 60},
        )
    ]


def test_default_downloader_rejects_non_transfernow_url(tmp_path: Path) -> None:
    importer, _, quarantine, _ = build_importer(tmp_path)

    with pytest.raises(SupervisedImportError, match="não é permitido"):
        importer._download_transfernow(
            "https://eviltransfernow.example/dl/token",
            "EXAME_FICTICIO.zip",
            quarantine,
        )

    assert not quarantine.exists()


def test_successful_flow_never_calls_delete_operations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, _, _, _ = build_importer(tmp_path)

    def forbidden_delete(*args, **kwargs):
        raise AssertionError("Nenhuma exclusão é permitida")

    monkeypatch.setattr(Path, "unlink", forbidden_delete)
    monkeypatch.setattr(Path, "rmdir", forbidden_delete)
    monkeypatch.setattr(shutil, "rmtree", forbidden_delete)

    result = importer.run(archive_path=archive)

    assert result.manifest_path.is_file()
    assert archive.exists()


def test_cli_dispatches_supervised_archive_mode_offline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import main
    from radiology import supervised_import

    calls = {}

    class FakeImporter:
        def __init__(self, **kwargs) -> None:
            calls["init"] = kwargs

        def run(self, **kwargs):
            calls["run"] = kwargs
            return SimpleNamespace(
                destination=tmp_path / "destination",
                manifest_path=tmp_path / "destination" / "manifest.json",
                onedrive_destination="Pacientes/PACIENTE/Radiologia/exame",
            )

    monkeypatch.setattr(
        supervised_import,
        "SupervisedRadiologyImporter",
        FakeImporter,
    )
    monkeypatch.setattr(main, "build_onedrive_graph_client", lambda: object())

    result = main.main(
        [
            "radiology-import-supervised",
            "--archive-path",
            str(tmp_path / "fixture.zip"),
            "--patient-source",
            "offline",
        ]
    )

    assert result == 0
    assert isinstance(
        calls["init"]["patient_repository"],
        EmptyPatientRepository,
    )
    assert calls["init"]["gmail_connector"] is None
    assert calls["run"] == {
        "archive_path": str(tmp_path / "fixture.zip"),
        "email_message_id": None,
    }


def test_cli_requires_exactly_one_input_option() -> None:
    import main

    assert main.main(["radiology-import-supervised"]) == 2
    assert main.main(
        [
            "radiology-import-supervised",
            "--archive-path",
            "fixture.zip",
            "--email-message-id",
            "fixture-message",
        ]
    ) == 2


def resolved_fixture(
    *, score: float = 1.0, reason=ResolutionReason.EXACT_NAME,
    patient_id: int | None = 990000010, candidate_count: int = 1,
    matched: bool = True, manual: bool = False,
) -> ResolvedPatient:
    return ResolvedPatient(
        patient_id=patient_id,
        patient_name=PATIENT_NAME if matched else None,
        matched=matched,
        requires_manual_review=manual,
        confidence_score=score,
        resolution_reason=reason,
        candidate_count=candidate_count,
        candidate_names=[PATIENT_NAME] * candidate_count,
        matched_by="exact_name" if matched else None,
    )


def test_unambiguous_selection_is_hands_off_even_with_legacy_flag_false(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, _, _, output = build_importer(tmp_path)

    manifest = json.loads(importer.run(archive_path=archive).manifest_path.read_text("utf-8"))

    assert manifest["selection_mode"] == {"patient": "auto", "folder": "auto"}
    assert not any("Candidatos de paciente" in line for line in output)


def test_exact_patient_and_single_folder_are_auto_selected(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, _, _, output = build_importer(
        tmp_path, answers=("CONFIRMAR",), auto_select_unambiguous=True,
    )

    manifest = json.loads(importer.run(archive_path=archive).manifest_path.read_text("utf-8"))

    assert manifest["selection_mode"] == {"patient": "auto", "folder": "auto"}
    assert manifest["auto_selection_reason"] == {
            "patient": "EXACT_NAME", "folder": "CONFIRMED_PATIENT_FOLDER"
    }
    assert any("Paciente selecionado automaticamente" in line for line in output)
    assert any("Destino remoto selecionado automaticamente" in line for line in output)


@pytest.mark.parametrize("score, expected", [(0.94, False), (0.95, True)])
def test_auto_selection_uses_independent_inclusive_threshold(
    tmp_path: Path, score: float, expected: bool,
) -> None:
    importer, _, _, _ = build_importer(tmp_path, auto_select_unambiguous=True)
    assert importer._is_patient_auto_selectable(resolved_fixture(score=score)) is expected


@pytest.mark.parametrize(
    "resolved",
    [
        resolved_fixture(candidate_count=2, manual=True, matched=False,
                         reason=ResolutionReason.MULTIPLE_HIGH_SCORE),
        resolved_fixture(candidate_count=1, manual=True, matched=False,
                         reason=ResolutionReason.MULTIPLE_HIGH_SCORE),
        resolved_fixture(patient_id=None),
        resolved_fixture(reason="FUTURE_UNKNOWN_REASON"),
    ],
)
def test_unsafe_patient_resolution_never_auto_selects(
    tmp_path: Path, resolved: ResolvedPatient,
) -> None:
    importer, _, _, _ = build_importer(tmp_path, auto_select_unambiguous=True)
    assert importer._is_patient_auto_selectable(resolved) is False


def test_patient_source_unavailable_stops_safely(tmp_path: Path) -> None:
    class UnavailableRepository:
        def find_candidates(self, name):
            raise PatientRepositoryUnavailableError("sensitive")

    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, patients_root, _, _ = build_importer(
        tmp_path, auto_select_unambiguous=True,
    )
    importer.patient_repository = UnavailableRepository()

    with pytest.raises(SupervisedImportError, match="indisponível"):
        importer.run(archive_path=archive)
    assert not (patients_root / PATIENT_NAME / "Exames de imagem").exists()


def test_invalid_patient_name_never_creates_staging_outside_root(tmp_path: Path) -> None:
    importer, _, _, _ = build_importer(tmp_path, auto_select_unambiguous=True)

    with pytest.raises(SupervisedImportError, match="inválido"):
        importer._choose_patient_folder("...", allow_auto=True)


def test_confirmed_patient_is_auto_selected_even_without_local_folders(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    folders = (f"001 - {PATIENT_NAME}", f"002 - {PATIENT_NAME}")
    importer, patients_root, _, _ = build_importer(
        tmp_path, answers=("CONFIRMAR",), patient_folders=folders,
        auto_select_unambiguous=True,
    )

    result = importer.run(archive_path=archive)
    manifest = json.loads(result.manifest_path.read_text("utf-8"))
    assert result.destination.is_relative_to(patients_root / PATIENT_NAME)
    assert manifest["selection_mode"] == {"patient": "auto", "folder": "auto"}


def test_legacy_local_folder_names_do_not_affect_remote_selection(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, _, _, _ = build_importer(
        tmp_path, answers=("CONFIRMAR",),
        patient_folders=("OUTRO PACIENTE",), auto_select_unambiguous=True,
    )
    result = importer.run(archive_path=archive)
    assert result.destination.parents[1].name == PATIENT_NAME


def test_force_manual_override_only_prompts_for_patient_decision(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, _, _, _ = build_importer(
        tmp_path, auto_select_unambiguous=True, force_manual_selection=True,
    )
    manifest = json.loads(importer.run(archive_path=archive).manifest_path.read_text("utf-8"))
    assert manifest["selection_mode"] == {"patient": "manual", "folder": "auto"}


def test_auto_selection_does_not_require_final_confirmation(tmp_path: Path) -> None:
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, patients_root, _, _ = build_importer(tmp_path, auto_select_unambiguous=True)
    importer.input = lambda prompt: pytest.fail(f"prompt inesperado: {prompt}")
    result = importer.run(archive_path=archive)
    assert result.destination.is_relative_to(patients_root / PATIENT_NAME / "Exames de imagem")


def test_selection_audit_is_sanitized(tmp_path: Path) -> None:
    stream = io.StringIO()
    logger = logging.Logger("selection-audit")
    logger.addHandler(logging.StreamHandler(stream))
    audit = AuditLogger(logger=logger, level="INFO", correlation_id="correlation-auto-0001")
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, _, _, _ = build_importer(
        tmp_path, answers=("CONFIRMAR",), auto_select_unambiguous=True,
        audit_logger=audit,
    )

    importer.run(archive_path=archive)
    payload = stream.getvalue()
    assert "PATIENT_AUTO_SELECTED" in payload
    assert "ONEDRIVE_FOLDER_AUTO_SELECTED" in payload
    assert '"confidence_score":1.0' in payload
    assert '"candidate_count":1' in payload
    assert '"folder_candidate_count":1' in payload
    assert '"override_manual":false' in payload
    assert PATIENT_NAME not in payload
    assert str(tmp_path) not in payload


def test_offline_mode_never_auto_selects(tmp_path: Path) -> None:
    importer, _, _, _ = build_importer(
        tmp_path, auto_select_unambiguous=True, patient_source_mode="offline",
    )
    assert importer._is_patient_auto_selectable(resolved_fixture()) is False


def test_cli_force_manual_option_reaches_importer(tmp_path: Path, monkeypatch) -> None:
    import main
    from radiology import supervised_import

    calls = {}

    class FakeImporter:
        def __init__(self, **kwargs):
            calls.update(kwargs)

        def run(self, **kwargs):
            return SimpleNamespace(
                destination=tmp_path / "destination",
                manifest_path=tmp_path / "destination" / "manifest.json",
                onedrive_destination="Pacientes/PACIENTE/Radiologia/exame",
            )

    monkeypatch.setattr(supervised_import, "SupervisedRadiologyImporter", FakeImporter)
    monkeypatch.setattr(main, "build_onedrive_graph_client", lambda: object())
    result = main.main([
        "radiology-import-supervised", "--archive-path", "fixture.zip",
        "--patient-source", "offline", "--force-manual-selection",
    ])
    assert result == 0
    assert calls["force_manual_selection"] is True


def test_completed_archive_is_blocked_by_default(tmp_path: Path) -> None:
    history = IntakeHistoryRepository(tmp_path / "intake.db")
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    first, patients_root, _, _ = build_importer(tmp_path, intake_history=history)
    first.run(archive_path=archive)
    second, _, _, output = build_importer(tmp_path, intake_history=history)

    with pytest.raises(SupervisedImportError, match="bloqueada por padrão"):
        second.run(archive_path=archive)

    assert any("já foi importado" in line for line in output)
    assert len(list((patients_root / PATIENT_NAME / "Exames de imagem").iterdir())) == 1
    assert history.list_records(limit=1)[0].status == "DUPLICATE_DETECTED"


def test_allow_reimport_without_exact_word_does_not_copy(tmp_path: Path) -> None:
    history = IntakeHistoryRepository(tmp_path / "intake.db")
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    first, patients_root, _, _ = build_importer(tmp_path, intake_history=history)
    first.run(archive_path=archive)
    second, _, _, _ = build_importer(
        tmp_path, answers=("",), intake_history=history, allow_reimport=True,
    )

    with pytest.raises(SupervisedImportCancelled, match="Reimportação cancelada"):
        second.run(archive_path=archive)
    assert len(list((patients_root / PATIENT_NAME / "Exames de imagem").iterdir())) == 1


def test_allow_reimport_with_two_explicit_confirmations_copies(tmp_path: Path) -> None:
    history = IntakeHistoryRepository(tmp_path / "intake.db")
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    first, _, _, _ = build_importer(tmp_path, intake_history=history)
    first.run(archive_path=archive)
    second, _, _, _ = build_importer(
        tmp_path, answers=("REIMPORTAR", "1", "1", "CONFIRMAR"),
        intake_history=history, allow_reimport=True,
    )

    result = second.run(archive_path=archive)
    manifest = json.loads(result.manifest_path.read_text("utf-8"))
    assert manifest["intake_record"]["reimport"] is True
    assert manifest["intake_record"]["duplicate_check"] == "confirmed"
    assert manifest["intake_record"]["previous_record_reference"].startswith("intake-")
    completed = history.list_records(status="COMPLETED")
    assert len(completed) == 2
    assert completed[0].reimport_confirmed == 1
    assert completed[0].previous_record_reference.startswith("intake-")


def test_history_failure_blocks_before_copy(tmp_path: Path) -> None:
    class UnavailableHistory:
        def create_downloaded(self, **kwargs):
            raise IntakeHistoryError("sensitive database path")

    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, patients_root, _, _ = build_importer(
        tmp_path, intake_history=UnavailableHistory(),
    )
    with pytest.raises(SupervisedImportError, match="histórico local"):
        importer.run(archive_path=archive)
    assert not (patients_root / PATIENT_NAME / "Exames de imagem").exists()


def test_intake_manifest_preserves_file_checksums(tmp_path: Path) -> None:
    history = IntakeHistoryRepository(tmp_path / "intake.db")
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    importer, _, _, _ = build_importer(tmp_path, intake_history=history)
    result = importer.run(archive_path=archive)
    manifest = json.loads(result.manifest_path.read_text("utf-8"))
    assert manifest["checksums"] == result.checksums
    assert manifest["intake_record"]["archive_sha256"] == importer._file_sha256(archive)
    assert manifest["intake_record"]["duplicate_check"] == "clear"


def test_duplicate_audit_contains_no_clinical_values(tmp_path: Path) -> None:
    stream = io.StringIO()
    logger = logging.Logger("duplicate-audit")
    logger.addHandler(logging.StreamHandler(stream))
    audit = AuditLogger(logger=logger, level="INFO", correlation_id="correlation-history-1")
    history = IntakeHistoryRepository(tmp_path / "intake.db")
    archive = create_zip(tmp_path / f"{PATIENT_NAME}_20991231.zip")
    first, _, _, _ = build_importer(tmp_path, intake_history=history, audit_logger=audit)
    first.run(archive_path=archive)
    second, _, _, _ = build_importer(tmp_path, intake_history=history, audit_logger=audit)
    with pytest.raises(SupervisedImportError):
        second.run(archive_path=archive)
    payload = stream.getvalue()
    assert "INTAKE_RECORD_CREATED" in payload
    assert "DUPLICATE_CONFIRMED" in payload
    assert PATIENT_NAME not in payload
    assert str(tmp_path) not in payload
