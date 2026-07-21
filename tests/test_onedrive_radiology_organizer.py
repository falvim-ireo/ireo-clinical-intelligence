"""Testes offline do organizador radiológico para OneDrive."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path

from integrations.onedrive_graph import GraphFolder, GraphUploadedItem
from models.patient import Patient
from radiology.dicom_reader import DicomSeries, DicomStudy
from radiology.patient_matcher import MatchStatus, PatientMatchResult
from radiology.transfernow_download import DownloadResult
from storage.onedrive_radiology_organizer import (
    OneDriveRadiologyOrganizer,
    OrganizerStatus,
)
from workflows.radiology_workflow import WorkflowResult


class FakeOneDriveClient:
    """Cliente Graph em memória, sem qualquer acesso de rede."""

    def __init__(self) -> None:
        self.root = GraphFolder("root-id", "Pasta pacientes 2026", drive_id="drive-id")
        self.folders: dict[tuple[str, str], GraphFolder] = {}
        self.children: dict[str, list[dict[str, object]]] = {"root-id": []}
        self.reports: dict[str, dict[str, object]] = {}
        self.uploads: list[tuple[str, str, bytes]] = []
        self.counter = 0

    def find_root_folder(self, configured_root: str) -> GraphFolder:
        assert configured_root == "Pasta pacientes 2026"
        return self.root

    def find_child_folder(
        self, parent: GraphFolder, name: str
    ) -> GraphFolder | None:
        return self.folders.get((parent.item_id, name.casefold()))

    def create_folder(self, parent: GraphFolder, name: str) -> GraphFolder:
        self.counter += 1
        folder = GraphFolder(f"folder-{self.counter}", name, drive_id="drive-id")
        self.folders[(parent.item_id, name.casefold())] = folder
        self.children.setdefault(folder.item_id, [])
        self.children.setdefault(parent.item_id, []).append(
            {"id": folder.item_id, "name": name, "folder": {}}
        )
        return folder

    def ensure_folder(self, parent: GraphFolder, name: str) -> GraphFolder:
        return self.find_child_folder(parent, name) or self.create_folder(parent, name)

    def list_children(self, folder: GraphFolder) -> list[dict[str, object]]:
        return list(self.children.get(folder.item_id, []))

    def upload_small_file(
        self,
        folder: GraphFolder,
        local_file: str | Path,
        remote_filename: str | None = None,
    ) -> GraphUploadedItem:
        path = Path(local_file)
        name = remote_filename or path.name
        content = path.read_bytes()
        item_id = f"upload-{len(self.uploads) + 1}"
        self.uploads.append((folder.item_id, name, content))
        self.children.setdefault(folder.item_id, []).append(
            {"id": item_id, "name": name, "file": {}}
        )
        if name == "report.json":
            self.reports[item_id] = json.loads(content)
        return GraphUploadedItem(name, len(content), True)

    def _get(self, path: str, *, params: dict[str, str]) -> dict[str, object]:
        del params
        item_id = path.split("/items/", 1)[1].split("/", 1)[0]
        return self.reports[item_id]

    def add_existing_study(
        self, patient_name: str, folder_name: str, study_uid: str
    ) -> GraphFolder:
        patient_folder = self.ensure_folder(self.root, patient_name)
        radiology = self.ensure_folder(patient_folder, "Radiologia")
        study_folder = self.create_folder(radiology, folder_name)
        report_id = "existing-report"
        self.children[study_folder.item_id].append(
            {"id": report_id, "name": "report.json", "file": {}}
        )
        self.reports[report_id] = {"study_instance_uid": study_uid}
        return study_folder


def workflow_result(
    tmp_path: Path,
    *,
    match_status: MatchStatus = MatchStatus.EXACT,
    study_date: str | None = "20260721",
    study_uid: str = "1.2.840.10008.1",
) -> WorkflowResult:
    """Cria um resultado completo sem executar o workflow real."""

    archive = tmp_path / "exame.zip"
    archive.write_bytes(b"zip original")
    patient = Patient(id=123, nome="PACIENTE TESTE")
    match = PatientMatchResult(
        status=match_status,
        score=1.0,
        candidates=(),
        selected_patient=patient if match_status is MatchStatus.EXACT else None,
        reasons=("fixture",),
    )
    series = DicomSeries(
        series_instance_uid="1.2.3.4",
        modality="CT",
        series_description="Axial",
        slice_thickness=1.0,
        pixel_spacing=(0.5, 0.5),
        image_count=12,
        files=[],
    )
    study = DicomStudy(
        patient_name="PACIENTE^TESTE",
        patient_id="123",
        study_date=study_date,
        study_description="TCFC",
        study_instance_uid=study_uid,
        manufacturer="Fabricante",
        manufacturer_model_name="Modelo",
        institution_name="IREO",
        modality="CT",
        series=[series],
    )
    return WorkflowResult(
        success=True,
        duration_seconds=1.25,
        download=DownloadResult(archive, archive.stat().st_size, "digest"),
        extraction=None,
        study=study,
        patient_match=match,
        errors=[],
    )


def organizer(client: FakeOneDriveClient) -> OneDriveRadiologyOrganizer:
    """Cria o organizador com data de processamento determinística."""

    return OneDriveRadiologyOrganizer(
        client,
        "Pasta pacientes 2026",
        processing_date=lambda: date(2026, 7, 22),
    )


def test_uploads_zip_and_report_into_expected_structure(tmp_path: Path) -> None:
    client = FakeOneDriveClient()

    result = organizer(client).organize(workflow_result(tmp_path))

    assert result.status is OrganizerStatus.UPLOADED
    assert result.remote_path == (
        "Pasta pacientes 2026/PACIENTE TESTE/Radiologia/2026-07-21 - TCFC"
    )
    assert result.drive_id == "drive-id"
    assert result.patient_folder_id is not None
    assert result.study_folder_id is not None
    assert result.uploaded_files == ("exame.zip", "report.json")
    assert [name for _, name, _ in client.uploads] == ["exame.zip", "report.json"]
    report = json.loads(client.uploads[1][2])
    assert report["study_instance_uid"] == "1.2.840.10008.1"
    assert report["series_count"] == 1
    assert report["image_count"] == 12


def test_uses_processing_date_when_study_date_is_absent(tmp_path: Path) -> None:
    client = FakeOneDriveClient()

    result = organizer(client).organize(
        workflow_result(tmp_path, study_date=None)
    )

    assert result.status is OrganizerStatus.UPLOADED
    assert result.remote_path is not None
    assert result.remote_path.endswith("Radiologia/2026-07-22 - TCFC")


def test_returns_review_required_without_exact_match(tmp_path: Path) -> None:
    client = FakeOneDriveClient()

    result = organizer(client).organize(
        workflow_result(tmp_path, match_status=MatchStatus.REVIEW_REQUIRED)
    )

    assert result.status is OrganizerStatus.REVIEW_REQUIRED
    assert client.uploads == []
    assert client.folders == {}


def test_detects_duplicate_by_study_instance_uid(tmp_path: Path) -> None:
    client = FakeOneDriveClient()
    existing = client.add_existing_study(
        "PACIENTE TESTE", "2026-07-21 - TCFC", "1.2.840.10008.1"
    )

    result = organizer(client).organize(workflow_result(tmp_path))

    assert result.status is OrganizerStatus.DUPLICATE
    assert result.study_folder_id == existing.item_id
    assert client.uploads == []


def test_never_overwrites_existing_folder_for_different_study(tmp_path: Path) -> None:
    client = FakeOneDriveClient()
    client.add_existing_study(
        "PACIENTE TESTE", "2026-07-21 - TCFC", "different-study-uid"
    )

    result = organizer(client).organize(workflow_result(tmp_path))

    assert result.status is OrganizerStatus.FAILED
    assert "sobrescrita recusada" in result.errors[0]
    assert client.uploads == []


def test_sanitizes_unexpected_external_error(tmp_path: Path) -> None:
    class FailingClient(FakeOneDriveClient):
        def find_root_folder(self, configured_root: str) -> GraphFolder:
            del configured_root
            raise RuntimeError("secret token and internal URL")

    result = organizer(FailingClient()).organize(workflow_result(tmp_path))

    assert result.status is OrganizerStatus.FAILED
    assert "RuntimeError" in result.errors[0]
    assert "secret" not in result.errors[0]
    assert "token" not in result.errors[0]
