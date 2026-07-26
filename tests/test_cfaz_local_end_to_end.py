"""Homologação local determinística do ciclo Cfaz base."""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import zipfile

import pytest
import requests

import main
from acquisition.base import (
    AcquiredPackage,
    AcquisitionAsset,
    AcquisitionRequest,
    AssetClassification,
)
from acquisition.cfaz_operations import CfazHistoryRepository
from acquisition.service import ProviderAcquisitionService
from core.config import Config
from integrations.onedrive_graph import GraphFolder, GraphUploadedItem
from models.patient import Patient
from observability.audit_logger import AuditLogger
from radiology.exam_index_service import ExamIndexService
from radiology.intake_history import IntakeHistoryRepository
from radiology.supervised_import import SupervisedRadiologyImporter
from repositories.patient_repository import InMemoryPatientRepository
from tests.synthetic_fixtures import (
    SYNTHETIC_CLINIC_ID,
    SYNTHETIC_INTERNAL_REQUEST_ID,
    SYNTHETIC_PATIENT_ALPHA,
    SYNTHETIC_PATIENT_ALPHA_ASCII,
    SYNTHETIC_PATIENT_ID,
    SYNTHETIC_REQUEST_ID_BETA,
    SYNTHETIC_SEQUENTIAL_ID,
    SYNTHETIC_STUDY_DATE,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jpeg(marker: bytes = b"synthetic-alpha") -> bytes:
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = (
        b"\xff\xc0\x00\x11\x08"
        + (800).to_bytes(2, "big")
        + (1200).to_bytes(2, "big")
        + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    )
    return b"\xff\xd8" + app0 + sof + marker + b"\xff\xd9"


SYNTHETIC_STL = (
    b"solid synthetic_fixture\n"
    b"facet normal 0 0 1\n"
    b" outer loop\n"
    b"  vertex 0 0 0\n"
    b"  vertex 1 0 0\n"
    b"  vertex 0 1 0\n"
    b" endloop\n"
    b"endfacet\n"
    b"endsolid synthetic_fixture\n"
)


class LocalCfazBoundary:
    """Fronteira Cfaz sintética; filesystem e pipeline internos continuam reais."""

    provider_id = "cfaz"
    provider_name = "Cfaz"

    def __init__(self) -> None:
        self.authenticate_calls = 0
        self.discover_calls: list[str] = []
        self.download_calls: list[str] = []
        self.finalized: list[tuple[str, bool]] = []

    def authenticate(self) -> None:
        self.authenticate_calls += 1

    def discover(self, notification) -> tuple[AcquisitionRequest, ...]:
        raise AssertionError("A homologação explícita não deve consultar Gmail.")

    def discover_request(self, identifier: str) -> tuple[AcquisitionRequest, ...]:
        self.discover_calls.append(identifier)
        return (self._request(identifier),)

    def download(
        self,
        request: AcquisitionRequest,
        quarantine_root: str | Path,
        correlation_id: str,
    ) -> AcquiredPackage:
        self.download_calls.append(request.request_id)
        target = Path(quarantine_root) / correlation_id
        target.mkdir(parents=True, exist_ok=True)
        archive = target / (
            f"{SYNTHETIC_PATIENT_ALPHA_ASCII}_{request.exam_date:%Y%m%d}_"
            f"CFAZ-{request.request_id}.zip"
        )
        is_control = request.request_id == SYNTHETIC_REQUEST_ID_BETA
        files = {
            "digital-models/modelo_sintetico.stl": (
                SYNTHETIC_STL.replace(b"vertex 1 0 0", b"vertex 2 0 0")
                if is_control
                else SYNTHETIC_STL
            ),
            "radiographs/panoramica_sintetica.jpg": _jpeg(
                b"synthetic-control" if is_control else b"synthetic-target"
            ),
        }
        with zipfile.ZipFile(archive, "w") as package:
            for name, content in files.items():
                package.writestr(name, content)
        metadata = (
            {
                "stored_name": "digital-models/modelo_sintetico.stl",
                "source_name": "modelo_sintetico.stl",
                "collection": "digital_models",
                "provider_section": "digital_models[].stl_files[]",
                "provider_display_name": "Modelo sintético",
                "detected_mime": "model/stl",
                "provider_exam_id": "synthetic-model",
                "provider_asset_id": "synthetic-stl",
            },
            {
                "stored_name": "radiographs/panoramica_sintetica.jpg",
                "source_name": "panoramica_sintetica.jpg",
                "collection": "panoramics",
                "provider_section": "images_download_links",
                "provider_display_name": "Panorâmica sintética",
                "detected_mime": "image/jpeg",
                "provider_exam_id": "synthetic-radiograph",
                "provider_asset_id": "synthetic-image",
            },
        )
        return AcquiredPackage(
            request=request,
            archive_path=archive,
            sha256=_sha256(archive),
            file_count=len(files),
            total_bytes=sum(map(len, files.values())),
            file_metadata=metadata,
        )

    def classify(self, filename: str, metadata=None) -> AssetClassification:
        return (
            AssetClassification.DIGITAL_MODEL
            if filename.casefold().endswith(".stl")
            else AssetClassification.PANORAMIC
        )

    def finalize(self, request: AcquisitionRequest, *, success: bool) -> None:
        self.finalized.append((request.request_id, success))

    @staticmethod
    def _request(identifier: str) -> AcquisitionRequest:
        is_control = identifier == SYNTHETIC_REQUEST_ID_BETA
        exam_day = 3 if is_control else 2
        return AcquisitionRequest(
            provider_id="cfaz",
            request_id=identifier,
            provider_request_id=identifier,
            sequential_id=(
                f"{SYNTHETIC_SEQUENTIAL_ID}2"
                if is_control
                else SYNTHETIC_SEQUENTIAL_ID
            ),
            clinic_number=SYNTHETIC_CLINIC_ID,
            source_url=f"https://max.cfaz.net/requests/{identifier}",
            patient_name=SYNTHETIC_PATIENT_ALPHA,
            request_date=datetime(2099, 1, exam_day, 11, tzinfo=timezone.utc),
            exam_date=datetime(2099, 1, exam_day, 12, tzinfo=timezone.utc),
            radiology_clinic="Clínica Sintética",
            professional="Profissional Sintético",
            provider_exam_id=f"synthetic-exam-{exam_day}",
            assets=(
                AcquisitionAsset(
                    asset_id="synthetic-stl",
                    download_url="https://files.cfaz.net/synthetic-model",
                    filename="modelo_sintetico.stl",
                    classification=AssetClassification.DIGITAL_MODEL,
                ),
                AcquisitionAsset(
                    asset_id="synthetic-image",
                    download_url="https://files.cfaz.net/synthetic-image",
                    filename="panoramica_sintetica.jpg",
                    classification=AssetClassification.PANORAMIC,
                ),
            ),
        )


class LocalOneDriveAdapter:
    """OneDrive local stateful com árvore, conteúdo e contagem de operações."""

    def __init__(self) -> None:
        self.root = GraphFolder("root", "Pacientes", drive_id="local-drive")
        self.folders: dict[tuple[str, str], GraphFolder] = {}
        self.children: dict[str, list[dict[str, object]]] = {"root": []}
        self.contents: dict[tuple[str, str], bytes] = {}
        self.uploads: list[tuple[str, str, str]] = []
        self.counter = 0

    def find_root_folder(self, configured_root: str) -> GraphFolder:
        assert configured_root == "Pacientes"
        return self.root

    def find_child_folder(
        self, parent: GraphFolder, name: str
    ) -> GraphFolder | None:
        return self.folders.get((parent.item_id, name.casefold()))

    def create_folder(self, parent: GraphFolder, name: str) -> GraphFolder:
        existing = self.find_child_folder(parent, name)
        if existing is not None:
            return existing
        self.counter += 1
        folder = GraphFolder(
            f"local-folder-{self.counter}", name, drive_id="local-drive"
        )
        self.folders[(parent.item_id, name.casefold())] = folder
        self.children.setdefault(parent.item_id, []).append(
            {"id": folder.item_id, "name": name, "folder": {}}
        )
        self.children.setdefault(folder.item_id, [])
        return folder

    def ensure_folder(self, parent: GraphFolder, name: str) -> GraphFolder:
        return self.find_child_folder(parent, name) or self.create_folder(
            parent, name
        )

    def list_children(self, folder: GraphFolder) -> list[dict[str, object]]:
        return list(self.children.get(folder.item_id, ()))

    def upload_small_file(
        self,
        folder: GraphFolder,
        local_file: str | Path,
        remote_filename: str | None = None,
        *,
        progress_callback=None,
        retry_callback=None,
    ) -> GraphUploadedItem:
        path = Path(local_file)
        name = remote_filename or path.name
        content = path.read_bytes()
        self.contents[(folder.item_id, name.casefold())] = content
        self.uploads.append(
            (folder.item_id, name, hashlib.sha256(content).hexdigest())
        )
        children = self.children.setdefault(folder.item_id, [])
        children[:] = [
            item
            for item in children
            if str(item.get("name") or "").casefold() != name.casefold()
        ]
        children.append(
            {
                "id": f"local-file-{len(self.uploads)}",
                "name": name,
                "size": len(content),
                "file": {},
            }
        )
        if progress_callback is not None:
            progress_callback(len(content), len(content))
        return GraphUploadedItem(name=name, size=len(content), has_id=True)

    def download_json_file(
        self, folder: GraphFolder, filename: str
    ) -> dict | None:
        content = self.contents.get((folder.item_id, filename.casefold()))
        return json.loads(content) if content is not None else None

    def state(self) -> tuple:
        return (
            tuple(sorted(self.folders)),
            tuple(
                sorted(
                    (key, hashlib.sha256(value).hexdigest())
                    for key, value in self.contents.items()
                )
            ),
        )


def _build_importer(
    *,
    root: Path,
    graph: LocalOneDriveAdapter,
    intake: IntakeHistoryRepository,
    index: ExamIndexService,
    correlation_id: str,
) -> SupervisedRadiologyImporter:
    tool = root / "synthetic-archive-tool"
    tool.write_bytes(b"synthetic-tool")
    return SupervisedRadiologyImporter(
        quarantine_root=root / "quarantine",
        archive_tool_path=tool,
        onedrive_client=graph,
        onedrive_root="Pacientes",
        patient_repository=InMemoryPatientRepository(
            [Patient(id=SYNTHETIC_PATIENT_ID, nome=SYNTHETIC_PATIENT_ALPHA)]
        ),
        audit_logger=AuditLogger(correlation_id=correlation_id),
        input_func=lambda prompt: pytest.fail(f"Interação inesperada: {prompt}"),
        output=lambda _: None,
        progress_output=lambda _: None,
        today_provider=lambda: date(2099, 1, 2),
        now_provider=lambda: datetime(2099, 1, 2, 12, tzinfo=timezone.utc),
        auto_select_unambiguous=True,
        auto_select_min_score=0.95,
        patient_source_mode="clinicorp",
        intake_history=intake,
        exam_index_service=index,
    )


def _service(
    *,
    provider: LocalCfazBoundary,
    importer: SupervisedRadiologyImporter,
    history: CfazHistoryRepository,
    quarantine: Path,
    correlation_id: str,
) -> ProviderAcquisitionService:
    return ProviderAcquisitionService(
        provider=provider,
        importer=importer,
        history=history,
        quarantine_root=quarantine,
        correlation_id=correlation_id,
        output=lambda _: None,
        monotonic_provider=iter((1.0, 2.0, 3.0, 4.0)).__next__,
    )


def _sqlite_rows(database: Path, query: str, values: tuple = ()) -> tuple:
    with sqlite3.connect(database) as connection:
        return tuple(connection.execute(query, values).fetchall())


def test_cfaz_local_cycle_import_reset_reimport_is_idempotent(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    def forbidden_network(*args, **kwargs):
        raise AssertionError("A homologação local não pode acessar a rede.")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden_network)
    monkeypatch.setattr(socket, "create_connection", forbidden_network)

    database = tmp_path / "clinical-index.db"
    intake_database = tmp_path / "intake.db"
    quarantine = tmp_path / "quarantine"
    graph = LocalOneDriveAdapter()
    provider = LocalCfazBoundary()
    history = CfazHistoryRepository(database)
    intake = IntakeHistoryRepository(intake_database)
    index = ExamIndexService(database)

    monkeypatch.setattr(
        Config, "IREO_RADIOLOGY_INDEX_DATABASE_PATH", str(database)
    )
    monkeypatch.setattr(Config, "IREO_INTAKE_DATABASE_PATH", str(intake_database))
    monkeypatch.setattr(Config, "IREO_RADIOLOGY_QUARANTINE_PATH", str(quarantine))

    initial_service = _service(
        provider=provider,
        importer=_build_importer(
            root=tmp_path,
            graph=graph,
            intake=intake,
            index=index,
            correlation_id="correlation-synthetic-target",
        ),
        history=history,
        quarantine=quarantine,
        correlation_id="correlation-synthetic-target",
    )
    target = initial_service.run_request_id(SYNTHETIC_INTERNAL_REQUEST_ID)[0]

    control_service = _service(
        provider=provider,
        importer=_build_importer(
            root=tmp_path,
            graph=graph,
            intake=intake,
            index=index,
            correlation_id="correlation-synthetic-control",
        ),
        history=history,
        quarantine=quarantine,
        correlation_id="correlation-synthetic-control",
    )
    control = control_service.run_request_id(SYNTHETIC_REQUEST_ID_BETA)[0]

    target_manifest = json.loads(target.manifest_path.read_text("utf-8"))
    control_manifest = json.loads(control.manifest_path.read_text("utf-8"))
    target_exam_id = target_manifest["publication"]["exam_id"]
    control_exam_id = control_manifest["publication"]["exam_id"]
    target_destination = target.onedrive_destination
    remote_before_reset = graph.state()
    uploads_before_reset = tuple(graph.uploads)

    assert target_manifest["publication"]["state"] == "COMPLETE"
    assert target_manifest["acquisition"]["clinical_package"]["provider"] == "cfaz"
    assert {
        item["clinical_category"]
        for item in target_manifest["acquisition"]["clinical_package"]["assets"]
    } == {"DIGITAL_MODEL", "RADIOGRAPH"}
    assert index.get_by_exam_id(target_exam_id) is not None
    assert len(index.clinical_assets(provider="cfaz")) == 4
    assert history.get_record(SYNTHETIC_INTERNAL_REQUEST_ID).status == "COMPLETE"
    assert len(intake.list_records(status="COMPLETED")) == 2

    semantic_before_dry_run = (
        target.manifest_path.read_bytes(),
        control.manifest_path.read_bytes(),
        graph.state(),
        _sqlite_rows(
            database,
            "SELECT request_id,status FROM cfaz_import_history ORDER BY request_id",
        ),
        _sqlite_rows(database, "SELECT exam_id FROM exams ORDER BY exam_id"),
        _sqlite_rows(
            intake_database,
            "SELECT correlation_id,status FROM radiology_imports ORDER BY id",
        ),
    )
    assert main.main(
        ["cfaz-reset", "--request-id", SYNTHETIC_INTERNAL_REQUEST_ID, "--dry-run"]
    ) == 0
    assert semantic_before_dry_run == (
        target.manifest_path.read_bytes(),
        control.manifest_path.read_bytes(),
        graph.state(),
        _sqlite_rows(
            database,
            "SELECT request_id,status FROM cfaz_import_history ORDER BY request_id",
        ),
        _sqlite_rows(database, "SELECT exam_id FROM exams ORDER BY exam_id"),
        _sqlite_rows(
            intake_database,
            "SELECT correlation_id,status FROM radiology_imports ORDER BY id",
        ),
    )

    assert main.main(
        ["cfaz-reset", "--request-id", SYNTHETIC_INTERNAL_REQUEST_ID, "--apply"]
    ) == 0
    assert not target.manifest_path.exists()
    assert control.manifest_path.exists()
    assert index.get_by_exam_id(target_exam_id) is None
    assert index.get_by_exam_id(control_exam_id) is not None
    assert history.get_record(
        SYNTHETIC_INTERNAL_REQUEST_ID
    ).status == "READY_FOR_REIMPORT"
    assert history.get_record(SYNTHETIC_REQUEST_ID_BETA).status == "COMPLETE"
    assert len(intake.list_records(status="RESET")) == 1
    assert len(intake.list_records(status="COMPLETED")) == 1
    assert graph.state() == remote_before_reset
    assert tuple(graph.uploads) == uploads_before_reset

    reimport_service = _service(
        provider=provider,
        importer=_build_importer(
            root=tmp_path,
            graph=graph,
            intake=intake,
            index=index,
            correlation_id="correlation-synthetic-reimport",
        ),
        history=history,
        quarantine=quarantine,
        correlation_id="correlation-synthetic-reimport",
    )
    reimported = reimport_service.run_request_id(SYNTHETIC_INTERNAL_REQUEST_ID)[0]
    reimported_manifest = json.loads(reimported.manifest_path.read_text("utf-8"))

    assert reimported_manifest["publication"]["exam_id"] == target_exam_id
    assert reimported.onedrive_destination == target_destination
    assert index.get_by_exam_id(target_exam_id) is not None
    assert index.get_by_exam_id(control_exam_id) is not None
    assert len(index.clinical_assets(provider="cfaz")) == 4
    assert history.get_record(SYNTHETIC_INTERNAL_REQUEST_ID).status == "COMPLETE"
    assert graph.state() == remote_before_reset
    assert tuple(graph.uploads) == uploads_before_reset
    assert provider.download_calls == [
        SYNTHETIC_INTERNAL_REQUEST_ID,
        SYNTHETIC_REQUEST_ID_BETA,
        SYNTHETIC_INTERNAL_REQUEST_ID,
    ]

    import acquisition.cfaz_provider as provider_module

    monkeypatch.setattr(provider_module, "CfazProvider", lambda **kwargs: provider)
    downloads_before_third_run = tuple(provider.download_calls)
    assert main.main(
        ["radiology-import-from-cfaz", "--request-id", SYNTHETIC_INTERNAL_REQUEST_ID]
    ) == 0
    assert tuple(provider.download_calls) == downloads_before_third_run
    assert "já foi importado" in capsys.readouterr().out
