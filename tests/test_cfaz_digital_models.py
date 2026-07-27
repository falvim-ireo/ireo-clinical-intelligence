"""Testes offline do fluxo suplementar de modelos digitais Cfaz."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import zipfile

import pytest

import main
from acquisition.base import AcquisitionRequest
from acquisition.cfaz_digital_models import (
    CfazDigitalModelError,
    CfazDigitalModelSupplement,
)
from acquisition.cfaz_operations import CfazHistoryRepository
from acquisition.cfaz_metadata_transaction import (
    LocalMetadataCoordinator,
    ProductiveMetadataSagaFactory,
    SupplementOperationRepository,
)
from acquisition.cfaz_provider import (
    CfazDigitalModelFile,
    CfazDigitalModelInventory,
    CfazProvider,
)
from integrations.onedrive_graph import (
    GraphCreatedItemReference,
    GraphRollbackVerification,
)


@pytest.fixture(autouse=True)
def allow_reserved_download_domain(monkeypatch):
    monkeypatch.setattr(
        CfazProvider,
        "_validate_download_url",
        classmethod(lambda _cls, _url: None),
    )
    monkeypatch.setattr(
        CfazProvider,
        "_is_digital_model_download_url",
        classmethod(
            lambda _cls, value: str(value).startswith(
                "https://models.example.invalid/"
            )
        ),
    )


def binary_stl(marker: bytes) -> bytes:
    header = marker[:80].ljust(80, b"\x00")
    return header + (1).to_bytes(4, "little") + b"\x00" * 50


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class Provider:
    def __init__(self, request: AcquisitionRequest, archives: dict[str, tuple[str, bytes]]):
        self.request = request
        self.archives = archives
        self.discover_calls = []
        self.download_calls = []
        self._session = ArchiveSession(self)

    def discover_digital_models(self, request_id):
        self.discover_calls.append(request_id)
        files = tuple(
            CfazDigitalModelFile(
                digital_model_id="synthetic-model",
                stl_file_id=file_id,
                download_url=f"https://models.example.invalid/bucket/{file_id}.zip?signature=synthetic",
                filename=f"{source_name}.zip",
                model_name="Escaneamento",
                source_field=f"digital_models[1].stl_files[{position}]",
            )
            for position, (file_id, (source_name, _)) in enumerate(
                self.archives.items(), 1
            )
        )
        return CfazDigitalModelInventory(
            request=self.request, model_count=1, files=files
        )

    def download_digital_model_archive(self, item, destination):
        self.download_calls.append(item.stl_file_id)
        source_name, content = self.archives[item.stl_file_id]
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr(f"{source_name}.stl", content)
        return sha256(destination.read_bytes()), destination.stat().st_size, "application/zip"


class UnsafeProvider(Provider):
    def __init__(self, request, archives):
        super().__init__(request, archives)
        self._session = UnsafeArchiveSession(self)

    def download_digital_model_archive(self, item, destination):
        self.download_calls.append(item.stl_file_id)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr("../escape.stl", binary_stl(b"unsafe"))
        return sha256(destination.read_bytes()), destination.stat().st_size, "application/zip"


class ArchiveResponse:
    status_code = 200

    def __init__(self, body):
        self.body = body
        self.headers = {
            "Content-Type": "application/zip",
            "Content-Length": str(len(body)),
        }

    def iter_content(self, chunk_size):
        for position in range(0, len(self.body), chunk_size):
            yield self.body[position:position + chunk_size]

    def close(self):
        pass


class ArchiveSession:
    def __init__(self, provider):
        self.provider = provider

    def get(self, url, **_kwargs):
        file_id = next(
            identity for identity in self.provider.archives
            if f"/{identity}.zip" in url
        )
        self.provider.download_calls.append(file_id)
        source_name, content = self.provider.archives[file_id]
        stream = BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr(f"{source_name}.stl", content)
        return ArchiveResponse(stream.getvalue())


class UnsafeArchiveSession(ArchiveSession):
    def get(self, url, **_kwargs):
        file_id = next(
            identity for identity in self.provider.archives
            if f"/{identity}.zip" in url
        )
        self.provider.download_calls.append(file_id)
        stream = BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("../escape.stl", binary_stl(b"unsafe"))
        return ArchiveResponse(stream.getvalue())


class Graph:
    VERIFIED_COMPENSATION_CAPABILITY = "graph-item-id-etag-delete-v1"

    def __init__(self, manifest):
        self.remote_manifest = deepcopy(manifest)
        self.uploads = []
        self.remote_models = {}

    def find_root_folder(self, path):
        return SimpleNamespace(name=path)

    def list_children(self, folder):
        path_children = {
            "Patients": ["Synthetic Patient"],
            "Synthetic Patient": ["Radiology"],
            "Radiology": ["Exam"],
            "Exam": ["04 - Modelos Digitais"],
        }
        if folder.name == "04 - Modelos Digitais":
            return [
                {
                    "id": name,
                    "name": name,
                    "size": size,
                    "file": {},
                }
                for name, size in self.remote_models.items()
            ]
        return [
            {"id": name, "name": name, "folder": {}}
            for name in path_children.get(folder.name, [])
        ]

    def folder_from_child_item(self, _folder, item):
        return SimpleNamespace(name=item["name"])

    def download_json_file(self, _folder, filename):
        assert filename == "manifest.json"
        return deepcopy(self.remote_manifest)

    def ensure_folder(self, _folder, name):
        return SimpleNamespace(name=name)

    def upload_small_file(self, folder, path, remote_filename=None):
        filename = remote_filename or path.name
        self.uploads.append((folder.name, filename))
        if filename == "manifest.json":
            self.remote_manifest = json.loads(Path(path).read_text("utf-8"))
        else:
            self.remote_models[filename] = Path(path).stat().st_size
        return SimpleNamespace(name=filename, size=Path(path).stat().st_size)

    def delete_child_file(self, _folder, filename):
        self.remote_models.pop(filename, None)

    def upload_small_file_transactional(
        self, folder, path, remote_filename=None
    ):
        uploaded = self.upload_small_file(folder, path, remote_filename)
        return GraphCreatedItemReference(
            drive_id="synthetic-drive",
            item_id=uploaded.name,
            etag="synthetic-etag",
        )

    def delete_created_item_verified(self, reference):
        self.remote_models.pop(reference.item_id, None)
        return GraphRollbackVerification(True, 204, 1)


class FailingGraph(Graph):
    def upload_small_file(self, folder, path, remote_filename=None):
        filename = remote_filename or path.name
        if filename == "manifest.json":
            raise RuntimeError("graph failure")
        return super().upload_small_file(folder, path, remote_filename)


class InterruptedGraph(Graph):
    def __init__(self, manifest):
        super().__init__(manifest)
        self.fail_next_checkpoint = False
        self.failed = False

    def upload_small_file(self, folder, path, remote_filename=None):
        filename = remote_filename or path.name
        if (
            filename == "manifest.json"
            and self.fail_next_checkpoint
            and not self.failed
        ):
            self.failed = True
            self.fail_next_checkpoint = False
            raise RuntimeError("checkpoint interrupted")
        result = super().upload_small_file(folder, path, remote_filename)
        if (
            filename != "manifest.json"
            and not self.failed
            and len(self.remote_models) == 1
        ):
            self.fail_next_checkpoint = True
        return result


class FailingArchiveSession(ArchiveSession):
    def __init__(self, provider, fail_on):
        super().__init__(provider)
        self.fail_on = fail_on
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        if self.calls == self.fail_on:
            return ArchiveResponse(b"<html>synthetic failure</html>")
        return super().get(url, **kwargs)


class FailingPromotionSupplement(CfazDigitalModelSupplement):
    def __init__(self, *args, fail_on, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_on = fail_on
        self.promotions = 0

    def _promote_local(self, source_path, target):
        self.promotions += 1
        if self.promotions == self.fail_on:
            raise OSError("synthetic promotion failure")
        return super()._promote_local(source_path, target)


class Index:
    def __init__(self):
        self.values = []

    def index_manifest(self, manifest, source):
        self.values.append((deepcopy(manifest), source))


class FailingIndex(Index):
    def index_manifest(self, manifest, source):
        if source != "cfaz-digital-models-rollback":
            raise RuntimeError("synthetic index failure")
        super().index_manifest(manifest, source)


class MetadataCoordinator:
    CAPABILITY = "cfaz-supplement-local-tx-v1"
    TEST_DOUBLE = True

    def __init__(self, index=None, events=None, fail_at=None):
        self.index = index
        self.events = events if events is not None else []
        self.fail_at = fail_at
        self.history_updates = []
        self.intake_updates = []
        self._snapshot = None
        self._manifest_path = None

    def preflight(self, **_kwargs):
        self.events.append("metadata_preflight")
        if self.fail_at == "preflight":
            raise RuntimeError("synthetic metadata preflight failure")

    def commit(
        self, *, operation_id, record, original_manifest, updated_manifest,
        manifest_path, created_files,
    ):
        self.events.append("metadata_commit")
        self._snapshot = manifest_path.read_bytes()
        self._manifest_path = manifest_path
        manifest_path.write_text(json.dumps(updated_manifest), "utf-8")
        if self.fail_at == "manifest":
            raise RuntimeError("synthetic manifest failure")
        if self.index is not None:
            self.index.index_manifest(
                updated_manifest, source="cfaz-digital-models"
            )
        if self.fail_at == "index":
            raise RuntimeError("synthetic index failure")
        self.history_updates.append(operation_id)
        if self.fail_at == "history":
            raise RuntimeError("synthetic history failure")
        self.intake_updates.append(operation_id)
        if self.fail_at == "intake":
            raise RuntimeError("synthetic intake failure")

    def rollback(self):
        self.events.append("metadata_rollback")
        if self._manifest_path is not None and self._snapshot is not None:
            self._manifest_path.write_bytes(self._snapshot)
        self.history_updates.clear()
        self.intake_updates.clear()


def fixture(tmp_path):
    database = tmp_path / "index.db"
    history = CfazHistoryRepository(database)
    history.mark_complete(
        request_id="99999991",
        provider_request_id="99999991",
        sequential_id="999991",
        clinic_number="99991",
        patient_name="Synthetic Patient",
        duration_seconds=1,
        onedrive_destination="Patients/Synthetic Patient/Radiology/Exam",
        acquisition_sha="a" * 64,
    )
    staging = tmp_path / "staging" / "Synthetic Patient" / "Exame"
    staging.mkdir(parents=True)
    prior = b"prior"
    (staging / "documentacao_001.jpg").write_bytes(prior)
    manifest = {
        "status": "COMPLETED",
        "file_count": 1,
        "total_size_bytes": len(prior),
        "onedrive_destination": "Patients/Synthetic Patient/Radiology/Exam",
        "acquisition": {
            "provider_id": "cfaz",
            "request_id": "99999991",
            "provider_request_id": "99999991",
            "sequential_id": "999991",
            "classifications": ["Imagem"],
            "asset_count": 1,
            "files": [{
                "stored_name": "documentacao_001.jpg",
                "relative_folder": "",
                "sha256": sha256(prior),
                "size_bytes": len(prior),
                "clinical_category": "DOCUMENTATION",
            }],
            "clinical_package": {"assets": []},
        },
        "checksums": {"documentacao_001.jpg": sha256(prior)},
        "publication": {
            "state": "COMPLETE",
            "exam_id": "e" * 64,
            "source_archive_sha256": "a" * 64,
            "total_files": 1,
            "total_bytes": len(prior),
            "uploaded_files_count": 1,
            "uploaded_bytes": len(prior),
            "uploaded_files": {
                "documentacao_001.jpg": {
                    "sha256": sha256(prior),
                    "size": len(prior),
                }
            },
        },
    }
    manifest_path = staging / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), "utf-8")
    request = AcquisitionRequest(
        provider_id="cfaz",
        request_id="99999991",
        provider_request_id="99999991",
        sequential_id="999991",
        clinic_number="99991",
        source_url="https://max.cfaz.net/requests/99999991",
        patient_name="Synthetic Patient",
        request_date=None,
        exam_date=None,
        radiology_clinic=None,
        professional=None,
        assets=(),
    )
    provider = Provider(request, {
        "synthetic-stl-alpha": ("LowerJawScan", binary_stl(b"lower")),
        "synthetic-stl-beta": ("UpperJawScan", binary_stl(b"upper")),
    })
    return history, staging, manifest_path, manifest, provider


def supplement(
    tmp_path, history, provider, graph=None, index=None,
    metadata=None, events=None,
):
    coordinator = metadata or MetadataCoordinator(index=index, events=events)
    return CfazDigitalModelSupplement(
        provider=provider,
        history=history,
        graph=graph,
        exam_index_service=index,
        metadata_coordinator=coordinator,
        event_recorder=(events.append if events is not None else None),
        allow_test_metadata_coordinator=True,
        staging_root=tmp_path / "staging",
        max_file_bytes=20 * 1024 * 1024,
        output=lambda _message: None,
        now_provider=lambda: datetime(2026, 7, 24, tzinfo=timezone.utc),
    )


def test_dry_run_enumerates_without_download_or_changes(tmp_path):
    history, staging, manifest_path, manifest, provider = fixture(tmp_path)
    before = manifest_path.read_bytes()
    before_paths = tuple(sorted(path.relative_to(staging) for path in staging.rglob("*")))
    events = []

    result = supplement(
        tmp_path, history, provider, graph=Graph(manifest), events=events
    ).run("999991")

    assert result.state == "DRY_RUN"
    assert result.model_count == 1
    assert result.provider_file_count == 2
    assert result.pending_file_count == 2
    assert len(result.planned_model_paths) == 2
    assert len(set(result.planned_model_paths)) == 2
    assert events == [
        "resolve_models",
        "local_preflight",
        "remote_preflight",
        "remote_preflight_complete",
    ]
    assert provider.download_calls == []
    assert manifest_path.read_bytes() == before
    assert tuple(
        sorted(path.relative_to(staging) for path in staging.rglob("*"))
    ) == before_paths


def test_history_read_only_lookup_does_not_modify_sqlite(tmp_path):
    history, _staging, _manifest_path, _manifest, _provider = fixture(tmp_path)
    database_path = history.database_path
    before = database_path.read_bytes()

    reopened = CfazHistoryRepository(database_path, read_only=True)

    assert reopened.get_record("999991") is not None
    assert database_path.read_bytes() == before


def test_history_read_only_supports_legacy_projection_without_migration(
    tmp_path,
):
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as db:
        db.execute(
            """CREATE TABLE cfaz_import_history (
            provider TEXT NOT NULL, request_id TEXT NOT NULL,
            provider_request_id TEXT, sequential_id TEXT,
            patient_name TEXT, status TEXT NOT NULL, started_at TEXT,
            completed_at TEXT, duration_seconds REAL,
            onedrive_destination TEXT, provider_exam_id TEXT,
            acquisition_sha TEXT, import_timestamp TEXT,
            updated_at TEXT NOT NULL)"""
        )
        db.execute(
            """INSERT INTO cfaz_import_history (
            provider,request_id,provider_request_id,sequential_id,
            patient_name,status,onedrive_destination,updated_at
            ) VALUES ('cfaz','internal','provider','sequence',
            'Synthetic Patient','COMPLETE','Patients/Synthetic','now')"""
        )
    before = database_path.read_bytes()

    record = CfazHistoryRepository(
        database_path, read_only=True
    ).get_record("sequence")

    assert record is not None
    assert record.supplement_operation_id is None
    assert record.supplement_payload_hash is None
    assert database_path.read_bytes() == before


def test_dry_run_remote_collision_fails_before_effects(tmp_path):
    history, staging, manifest_path, manifest, provider = fixture(tmp_path)
    graph = Graph(manifest)
    first = supplement(tmp_path, history, provider, graph=graph).run("999991")
    graph.remote_models[first.planned_model_paths[0].rsplit("/", 1)[-1]] = 123
    before = manifest_path.read_bytes()
    before_paths = tuple(sorted(path.relative_to(staging) for path in staging.rglob("*")))

    with pytest.raises(CfazDigitalModelError, match="remoto"):
        supplement(tmp_path, history, provider, graph=graph).run("999991")

    assert provider.download_calls == []
    assert graph.uploads == []
    assert manifest_path.read_bytes() == before
    assert tuple(
        sorted(path.relative_to(staging) for path in staging.rglob("*"))
    ) == before_paths


def test_cli_exposes_explicit_and_mutually_exclusive_apply(capsys):
    assert main.main(["cfaz-digital-models", "--help"]) == 0
    output = capsys.readouterr().out
    assert "--dry-run" in output
    assert "--apply" in output


@pytest.mark.parametrize(
    "coordinator",
    [
        None,
        object(),
        SimpleNamespace(
            CAPABILITY=LocalMetadataCoordinator.CAPABILITY,
            PRODUCTIVE_IMPLEMENTATION=True,
            create=lambda **_kwargs: None,
        ),
    ],
)
def test_cli_apply_gate_blocks_before_graph_auth_browser_or_run(
    monkeypatch, capsys, coordinator,
):
    effects = []
    monkeypatch.setattr(
        main,
        "_resolve_cfaz_productive_metadata_coordinator",
        lambda: coordinator,
    )
    monkeypatch.setattr(
        main,
        "build_onedrive_graph_client",
        lambda: effects.append("graph") or object(),
    )

    result = main.main([
        "cfaz-digital-models",
        "--request-id", "999991",
        "--browser-session",
        "--apply",
    ])

    assert result == 1
    assert effects == []
    assert "aplicação bloqueada" in capsys.readouterr().out


def test_cli_dry_run_uses_read_only_history_and_graph(monkeypatch):
    events = []
    read_only_graph = object()

    class FakeHistory:
        def __init__(self, _path, *, read_only=False):
            events.append(("history", read_only))

    class FakeProvider:
        def __init__(self, **_kwargs):
            events.append(("provider", True))

    class FakeGraph:
        def read_only(self):
            events.append(("graph_read_only", True))
            return read_only_graph

    class FakeSupplement:
        def __init__(self, **kwargs):
            assert kwargs["graph"] is read_only_graph
            assert kwargs["metadata_coordinator"] is None

        def run(self, _request_id, *, apply):
            assert apply is False
            events.append(("run", True))
            return SimpleNamespace(
                state="DRY_RUN",
                added_file_count=0,
                reused_file_count=0,
            )

    import acquisition.cfaz_digital_models as digital_models_module
    import acquisition.cfaz_operations as operations_module
    import acquisition.cfaz_provider as provider_module

    monkeypatch.setattr(operations_module, "CfazHistoryRepository", FakeHistory)
    monkeypatch.setattr(provider_module, "CfazProvider", FakeProvider)
    monkeypatch.setattr(
        digital_models_module, "CfazDigitalModelSupplement", FakeSupplement
    )
    monkeypatch.setattr(main, "build_onedrive_graph_client", FakeGraph)

    assert main.main([
        "cfaz-digital-models", "--request-id", "999991", "--dry-run",
    ]) == 0
    assert events == [
        ("history", True),
        ("provider", True),
        ("graph_read_only", True),
        ("run", True),
    ]


def test_cli_injected_capable_coordinator_orders_gate_before_graph_and_run(
    monkeypatch, tmp_path,
):
    events = []
    coordinator = ProductiveMetadataSagaFactory(
        radiology_database_path=tmp_path / "radiology.db",
        intake_database_path=tmp_path / "intake.db",
    )

    class FakeHistory:
        def __init__(self, _path, **_kwargs):
            events.append("history")

    class FakeProvider:
        def __init__(self, **_kwargs):
            events.append("provider")

    class FakeIndex:
        def __init__(self, _path):
            events.append("index")

    class FakeSupplement:
        def __init__(self, **kwargs):
            assert kwargs["metadata_coordinator"] is coordinator
            events.append("supplement")

        def run(self, _request_id, *, apply):
            assert apply is True
            events.append("run")
            return SimpleNamespace(
                state="COMPLETE",
                added_file_count=2,
                reused_file_count=0,
            )

    import acquisition.cfaz_digital_models as digital_models_module
    import acquisition.cfaz_operations as operations_module
    import acquisition.cfaz_provider as provider_module
    import radiology.exam_index_service as index_module

    monkeypatch.setattr(
        main,
        "_resolve_cfaz_productive_metadata_coordinator",
        lambda: events.append("gate") or coordinator,
    )
    monkeypatch.setattr(
        main,
        "build_onedrive_graph_client",
        lambda: events.append("graph") or object(),
    )
    monkeypatch.setattr(
        operations_module, "CfazHistoryRepository", FakeHistory
    )
    monkeypatch.setattr(provider_module, "CfazProvider", FakeProvider)
    monkeypatch.setattr(index_module, "ExamIndexService", FakeIndex)
    monkeypatch.setattr(
        digital_models_module, "CfazDigitalModelSupplement", FakeSupplement
    )
    assert main.main([
        "cfaz-digital-models", "--request-id", "999991", "--apply",
    ]) == 0
    assert events.index("gate") < events.index("graph") < events.index("run")


def test_productive_resolver_returns_real_saga_factory_without_io(
    monkeypatch, tmp_path,
):
    from core.config import Config

    radiology = tmp_path / "radiology.db"
    intake = tmp_path / "intake.db"
    monkeypatch.setattr(
        Config, "IREO_RADIOLOGY_INDEX_DATABASE_PATH", str(radiology)
    )
    monkeypatch.setattr(Config, "IREO_INTAKE_DATABASE_PATH", str(intake))

    resolved = main._resolve_cfaz_productive_metadata_coordinator()

    assert isinstance(resolved, ProductiveMetadataSagaFactory)
    assert resolved.CAPABILITY == LocalMetadataCoordinator.CAPABILITY
    assert not radiology.exists()
    assert not intake.exists()


def test_apply_adds_two_stl_updates_remote_manifest_and_is_idempotent(tmp_path):
    history, staging, manifest_path, manifest, provider = fixture(tmp_path)
    graph = Graph(manifest)
    index = Index()
    service = supplement(tmp_path, history, provider, graph, index)

    first = service.run("999991", apply=True)

    assert first.state == "COMPLETE"
    assert first.added_file_count == 2
    lower = staging / "04 - Modelos Digitais" / "modelo_mandibula_001.stl"
    upper = staging / "04 - Modelos Digitais" / "modelo_maxila_001.stl"
    assert lower.read_bytes() == binary_stl(b"lower")
    assert upper.read_bytes() == binary_stl(b"upper")
    updated = json.loads(manifest_path.read_text("utf-8"))
    marker = updated["cfaz_digital_models"]
    assert marker["state"] == "COMPLETE"
    assert sorted(marker["provider_files"]) == ["synthetic-stl-alpha", "synthetic-stl-beta"]
    assert all(
        item["remote_uploaded"] for item in marker["provider_files"].values()
    )
    assert updated["publication"]["state"] == "COMPLETE"
    assert updated["publication"]["total_files"] == 3
    assert updated["file_count"] == 3
    package_assets = updated["acquisition"]["clinical_package"]["assets"]
    assert {item["stl_file_id"] for item in package_assets} == {
        "synthetic-stl-alpha", "synthetic-stl-beta",
    }
    serialized = json.dumps(updated)
    assert "models.example.invalid" not in serialized
    assert "Signature" not in serialized
    assert history.get_record("999991").status == "COMPLETE"
    assert index.values[-1][1] == "cfaz-digital-models"
    assert set(graph.remote_models) == {
        "modelo_mandibula_001.stl",
        "modelo_maxila_001.stl",
    }

    provider.discover_digital_models = lambda _value: (_ for _ in ()).throw(
        AssertionError("provider não deve ser consultado")
    )
    download_count = len(provider.download_calls)
    upload_count = len(graph.uploads)

    second = service.run("99999991", apply=True)

    assert second.state == "ALREADY_COMPLETE"
    assert len(provider.download_calls) == download_count
    assert len(graph.uploads) == upload_count


def test_productive_saga_executes_then_resumes_without_new_remote_effects(
    tmp_path, monkeypatch,
):
    history, _staging, manifest_path, manifest, provider = fixture(tmp_path)
    graph = Graph(manifest)
    radiology_database = history.database_path
    factory = ProductiveMetadataSagaFactory(
        radiology_database_path=radiology_database,
        intake_database_path=tmp_path / "intake.db",
    )
    calls = []
    original_execute = LocalMetadataCoordinator.execute
    original_resume = LocalMetadataCoordinator.resume
    monkeypatch.setattr(
        LocalMetadataCoordinator,
        "execute",
        lambda self: calls.append(("execute", self.operation_id))
        or original_execute(self),
    )
    monkeypatch.setattr(
        LocalMetadataCoordinator,
        "resume",
        lambda self: calls.append(("resume", self.operation_id))
        or original_resume(self),
    )
    service = CfazDigitalModelSupplement(
        provider=provider,
        history=history,
        graph=graph,
        metadata_coordinator=factory,
        staging_root=tmp_path / "staging",
        max_file_bytes=10_000_000,
        output=lambda _message: None,
    )

    first = service.run("999991", apply=True)
    first_downloads = len(provider.download_calls)
    first_uploads = len(graph.uploads)
    provider.discover_digital_models = lambda _value: (_ for _ in ()).throw(
        AssertionError("provider não deve ser consultado")
    )
    second = service.run("99999991", apply=True)

    assert first.state == "COMPLETE"
    assert second.state == "ALREADY_COMPLETE"
    assert [name for name, _operation_id in calls] == ["execute", "resume"]
    assert calls[0][1] == calls[1][1]
    assert len(provider.download_calls) == first_downloads
    assert len(graph.uploads) == first_uploads
    persisted = SupplementOperationRepository(
        str(radiology_database)
    ).get(calls[0][1])
    assert persisted is not None and persisted.phase == "COMPLETED"
    projection = json.loads(persisted.prepared_delta_json)
    assert (
        projection["cfaz_digital_models"]["operation_id"]
        == calls[0][1]
    )
    assert len(projection["cfaz_digital_models"]["provider_files"]) == 2
    assert "operation_id" not in json.loads(
        manifest_path.read_text("utf-8")
    )["cfaz_digital_models"]
    serialized_projection = persisted.prepared_delta_json.casefold()
    assert "://" not in serialized_projection
    assert "token" not in serialized_projection
    assert "graph" not in serialized_projection
    assert "solid " not in serialized_projection


def test_unsafe_zip_is_rejected_before_manifest_or_destination_change(tmp_path):
    history, staging, manifest_path, manifest, provider = fixture(tmp_path)
    provider = UnsafeProvider(provider.request, provider.archives)
    before = manifest_path.read_bytes()

    with pytest.raises(CfazDigitalModelError, match="validação estrutural"):
        supplement(
            tmp_path, history, provider, Graph(manifest), Index()
        ).run("999991", apply=True)

    assert manifest_path.read_bytes() == before
    assert not (staging / "04 - Modelos Digitais").exists()
    assert not (tmp_path / "escape.stl").exists()


def test_failure_before_first_remote_file_rolls_back_local_changes(tmp_path):
    history, staging, manifest_path, manifest, provider = fixture(tmp_path)
    before = manifest_path.read_bytes()

    with pytest.raises(CfazDigitalModelError, match="estado seguro"):
        supplement(
            tmp_path, history, provider, FailingGraph(manifest), Index()
        ).run("999991", apply=True)

    assert manifest_path.read_bytes() == before
    assert not (staging / "04 - Modelos Digitais").exists()


def test_interruption_after_first_upload_rolls_back_without_duplicate(tmp_path):
    history, _staging, manifest_path, manifest, provider = fixture(tmp_path)
    graph = InterruptedGraph(manifest)
    service = supplement(tmp_path, history, provider, graph, Index())

    with pytest.raises(CfazDigitalModelError, match="estado seguro"):
        service.run("999991", apply=True)

    assert json.loads(manifest_path.read_text("utf-8")) == manifest
    assert graph.remote_models == {}


@pytest.mark.parametrize("fail_on", [1, 2])
def test_download_failure_rolls_back_all_local_and_remote_state(
    tmp_path, fail_on,
):
    history, staging, manifest_path, manifest, provider = fixture(tmp_path)
    provider._session = FailingArchiveSession(provider, fail_on)

    with pytest.raises(CfazDigitalModelError, match="download"):
        supplement(
            tmp_path, history, provider, Graph(manifest), Index()
        ).run("999991", apply=True)

    assert json.loads(manifest_path.read_text("utf-8")) == manifest
    assert not (staging / "04 - Modelos Digitais").exists()
    assert not (tmp_path / "cfaz-digital-models").exists()


@pytest.mark.parametrize("fail_on", [1, 2])
def test_local_promotion_failure_rolls_back_both_assets(tmp_path, fail_on):
    history, staging, manifest_path, manifest, provider = fixture(tmp_path)
    graph = Graph(manifest)
    service = FailingPromotionSupplement(
        provider=provider,
        history=history,
        graph=graph,
        exam_index_service=Index(),
        metadata_coordinator=MetadataCoordinator(index=Index()),
        allow_test_metadata_coordinator=True,
        staging_root=tmp_path / "staging",
        max_file_bytes=20 * 1024 * 1024,
        output=lambda _message: None,
        fail_on=fail_on,
    )

    with pytest.raises(CfazDigitalModelError, match="staging local"):
        service.run("999991", apply=True)

    assert json.loads(manifest_path.read_text("utf-8")) == manifest
    assert graph.remote_models == {}
    assert not (staging / "04 - Modelos Digitais").exists()


def test_index_failure_restores_manifest_files_and_remote_models(tmp_path):
    history, staging, manifest_path, manifest, provider = fixture(tmp_path)
    graph = Graph(manifest)

    with pytest.raises(CfazDigitalModelError, match="estado seguro"):
        supplement(
            tmp_path, history, provider, graph, FailingIndex()
        ).run("999991", apply=True)

    assert json.loads(manifest_path.read_text("utf-8")) == manifest
    assert graph.remote_manifest == manifest
    assert graph.remote_models == {}
    assert not (staging / "04 - Modelos Digitais").exists()


def test_duplicate_urls_fail_before_first_get(tmp_path):
    history, _staging, _path, manifest, provider = fixture(tmp_path)
    original_discover = provider.discover_digital_models

    def discover(request_id):
        inventory = original_discover(request_id)
        shared = inventory.files[0].download_url
        return replace(
            inventory,
            files=tuple(
                replace(item, download_url=shared) for item in inventory.files
            ),
        )

    provider.discover_digital_models = discover

    with pytest.raises(CfazDigitalModelError, match="ambíguo"):
        supplement(
            tmp_path, history, provider, Graph(manifest), Index()
        ).run("999991", apply=True)

    assert provider.download_calls == []


def test_apply_preflight_accepts_only_verified_compensation_capability(tmp_path):
    history, _staging, _path, manifest, provider = fixture(tmp_path)
    inventory = provider.discover_digital_models("999991")
    service = supplement(tmp_path, history, provider, Graph(manifest), Index())

    service._validate_apply_preflight(inventory)


def test_apply_preflight_rejects_client_with_delete_method_only(tmp_path):
    history, _staging, _path, _manifest, provider = fixture(tmp_path)
    inventory = provider.discover_digital_models("999991")

    class DeleteOnly:
        def delete_created_item_verified(self, _reference):
            raise AssertionError("must not be called")

    service = supplement(tmp_path, history, provider, DeleteOnly(), Index())

    with pytest.raises(CfazDigitalModelError, match="rollback verificável"):
        service._validate_apply_preflight(inventory)


def test_identical_contents_for_distinct_ids_require_review(tmp_path):
    history, staging, manifest_path, manifest, provider = fixture(tmp_path)
    same = binary_stl(b"same-synthetic-model")
    provider.archives = {
        identity: (name, same)
        for identity, (name, _content) in provider.archives.items()
    }

    with pytest.raises(CfazDigitalModelError, match="conteúdo idêntico"):
        supplement(
            tmp_path, history, provider, Graph(manifest), Index()
        ).run("999991", apply=True)

    assert json.loads(manifest_path.read_text("utf-8")) == manifest
    assert not (staging / "04 - Modelos Digitais").exists()


def test_partial_preexisting_identity_requires_review_without_get(tmp_path):
    history, staging, manifest_path, manifest, provider = fixture(tmp_path)
    partial = deepcopy(manifest)
    partial["acquisition"]["assets"] = [{
        "provider": "cfaz",
        "provider_asset_id": "synthetic-stl-alpha",
        "stl_file_id": "synthetic-stl-alpha",
        "stored_name": "synthetic-partial.stl",
        "relative_folder": "04 - Modelos Digitais",
        "sha256": "a" * 64,
    }]
    manifest_path.write_text(json.dumps(partial), "utf-8")

    with pytest.raises(CfazDigitalModelError, match="estado parcial"):
        supplement(
            tmp_path, history, provider, Graph(manifest), Index()
        ).run("999991", apply=True)

    assert provider.download_calls == []
    assert not (staging / "04 - Modelos Digitais").exists()


def test_transaction_phases_are_effectively_ordered_and_metadata_is_coordinated(
    tmp_path,
):
    history, _staging, _path, manifest, provider = fixture(tmp_path)
    events = []
    metadata = MetadataCoordinator(index=Index(), events=events)

    result = supplement(
        tmp_path, history, provider, Graph(manifest), Index(),
        metadata=metadata, events=events,
    ).run("999991", apply=True)

    assert result.state == "COMPLETE"
    assert events == [
        "resolve_models",
        "local_preflight",
        "metadata_preflight",
        "remote_preflight",
        "remote_preflight_complete",
        "staging_acquired",
        "downloads_validated",
        "remote_persisted",
        "local_promotion",
        "metadata_commit",
        "metadata_committed",
    ]
    assert len(metadata.history_updates) == 1
    assert metadata.history_updates == metadata.intake_updates


def test_remote_collision_blocks_download_and_staging_creation(tmp_path):
    history, _staging, _path, manifest, provider = fixture(tmp_path)
    graph = Graph(manifest)
    graph.remote_models["modelo_mandibula_001.stl"] = 123
    staging_root = tmp_path / "staging"

    with pytest.raises(CfazDigitalModelError, match="remoto"):
        supplement(
            tmp_path, history, provider, graph, Index()
        ).run("999991", apply=True)

    assert provider.download_calls == []
    assert not list(staging_root.glob(".cfaz-models-transaction-*"))


@pytest.mark.parametrize("mode", ["missing", "transport"])
def test_unprovable_remote_destination_blocks_before_download_or_staging(
    tmp_path, mode,
):
    history, _staging, _path, manifest, provider = fixture(tmp_path)

    class UnprovableGraph(Graph):
        def list_children(self, folder):
            if mode == "missing" and folder.name == "Exam":
                return []
            return super().list_children(folder)

        def download_json_file(self, folder, filename):
            if mode == "transport":
                raise TimeoutError("synthetic transport failure")
            return super().download_json_file(folder, filename)

    with pytest.raises(CfazDigitalModelError, match="remot"):
        supplement(
            tmp_path, history, provider, UnprovableGraph(manifest), Index()
        ).run("999991", apply=True)

    assert provider.download_calls == []
    assert not list((tmp_path / "staging").glob(".cfaz-models-transaction-*"))


def test_transaction_staging_is_exclusive_and_preserves_preexisting_content(
    tmp_path,
):
    history, _staging, _path, manifest, provider = fixture(tmp_path)
    staging_root = tmp_path / "staging"
    sentinel = staging_root / "preexisting-synthetic-state.txt"
    sentinel.write_text("preserve", "utf-8")

    supplement(
        tmp_path, history, provider, Graph(manifest), Index()
    ).run("999991", apply=True)

    assert sentinel.read_text("utf-8") == "preserve"
    assert not list(staging_root.glob(".cfaz-models-transaction-*"))


@pytest.mark.parametrize("fail_at", ["manifest", "index", "history", "intake"])
def test_metadata_failure_restores_manifest_history_intake_and_files(
    tmp_path, fail_at,
):
    history, staging, manifest_path, manifest, provider = fixture(tmp_path)
    graph = Graph(manifest)
    metadata = MetadataCoordinator(index=Index(), fail_at=fail_at)

    with pytest.raises(CfazDigitalModelError, match="estado seguro"):
        supplement(
            tmp_path, history, provider, graph, Index(), metadata=metadata
        ).run("999991", apply=True)

    assert json.loads(manifest_path.read_text("utf-8")) == manifest
    assert metadata.history_updates == []
    assert metadata.intake_updates == []
    assert graph.remote_models == {}
    assert not (staging / "04 - Modelos Digitais").exists()
    assert not list((tmp_path / "staging").glob(".cfaz-models-transaction-*"))


def test_apply_is_blocked_without_executable_local_transaction_capability(
    tmp_path,
):
    history, _staging, _path, manifest, provider = fixture(tmp_path)
    service = CfazDigitalModelSupplement(
        provider=provider,
        history=history,
        graph=Graph(manifest),
        metadata_coordinator=object(),
        staging_root=tmp_path / "staging",
        output=lambda _message: None,
    )

    with pytest.raises(CfazDigitalModelError, match="Saga durável produtiva"):
        service.run("999991", apply=True)

    assert provider.discover_calls == []
    assert provider.download_calls == []


class HttpResponse:
    def __init__(self, status, payload, headers=None):
        self.status_code = status
        self.payload = payload
        self.headers = headers or {"Content-Type": "application/json"}

    def json(self):
        return self.payload


class HttpSession:
    def __init__(self, api_payload, page_payload):
        self.api_payload = api_payload
        self.page_payload = page_payload
        self.calls = []
        self.cookies = {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        authenticated = kwargs.get("params", {}).get("access_token") == "token"
        if not authenticated:
            return HttpResponse(401, {})
        if url.endswith("/api/v1/requests/99999991"):
            return HttpResponse(200, self.api_payload)
        if url.endswith("/requests/99999991.json"):
            return HttpResponse(200, self.page_payload)
        if "/digital_models/99999992" in url:
            return HttpResponse(200, self.page_payload)
        raise AssertionError(url)


def test_provider_resolves_two_signed_urls_from_authenticated_page_json():
    api_payload = {
        "id": 99999991,
        "sequential_id": 999991,
        "clinic_id": 99991,
        "patient_datum": {"name": "Synthetic Patient"},
        "digital_models": [{
            "id": 99999992,
            "model_name": "Escaneamento",
            "stl_files": [
                {
                    "id": 99999993,
                    "digital_model_id": 99999992,
                },
                {
                    "id": 99999994,
                    "digital_model_id": 99999992,
                },
            ],
        }],
    }
    page_payload = {
        "digital_models": [{
            "id": 99999992,
            "stl_files": [
                {
                    "id": 99999993,
                    "name": "LowerJawScan.zip",
                    "download_url": (
                        "https://models.example.invalid/bucket/lower.zip"
                        "?GoogleAccessId=x&Expires=1&signature=synthetic"
                    ),
                },
                {
                    "id": 99999994,
                    "name": "UpperJawScan.zip",
                    "download_url": (
                        "https://models.example.invalid/bucket/upper.zip"
                        "?GoogleAccessId=x&Expires=1&signature=synthetic"
                    ),
                },
            ],
        }],
    }
    session = HttpSession(api_payload, page_payload)
    output = []
    provider = CfazProvider(
        api_token="token", session=session, output=output.append
    )

    inventory = provider.discover_digital_models("99999991")

    assert inventory.model_count == 1
    assert [item.stl_file_id for item in inventory.files] == [
        "99999993", "99999994",
    ]
    assert [item.filename for item in inventory.files] == [
        "LowerJawScan.zip", "UpperJawScan.zip",
    ]
    assert len(session.calls) == 4
    assert all("Signature" not in line for line in output)


def test_provider_uses_configured_session_login_when_page_rejects_api_token():
    api_payload = {
        "id": 99999991,
        "sequential_id": 999991,
        "digital_models": [{
            "id": 99999992,
            "stl_files": [{"id": 99999993}],
        }],
    }
    page_payload = {
        "stl_files": [{
            "id": 99999993,
            "download_url": (
                "https://models.example.invalid/bucket/lower.zip"
                "?GoogleAccessId=x&Expires=1&signature=synthetic"
            ),
        }],
    }

    class CredentialSession:
        cookies = {}

        def __init__(self):
            self.calls = []
            self.login_calls = 0

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if url.endswith("/api/v1/requests/99999991"):
                if kwargs.get("params", {}).get("access_token") == "token":
                    return HttpResponse(200, api_payload)
                return HttpResponse(401, {})
            if url.endswith("/requests/99999991.json"):
                if "access-token" in kwargs.get("headers", {}):
                    return HttpResponse(200, page_payload)
                return HttpResponse(403, {})
            if "/digital_models/99999992" in url:
                if "access-token" in kwargs.get("headers", {}):
                    return HttpResponse(200, page_payload)
                return HttpResponse(403, {})
            raise AssertionError(url)

        def post(self, url, **kwargs):
            self.login_calls += 1
            assert url.endswith("/api/v1/auth/sign_in")
            return HttpResponse(200, {}, headers={
                "access-token": "session-token",
                "client": "client",
                "uid": "fixture1@example.com",
                "expiry": "9999999999",
            })

    session = CredentialSession()
    provider = CfazProvider(
        api_token="token",
        email="fixture1@example.com",
        password="password",
        session=session,
        output=lambda _message: None,
    )

    inventory = provider.discover_digital_models("99999991")

    assert [item.stl_file_id for item in inventory.files] == ["99999993"]
    assert session.login_calls == 1


def test_provider_uses_browser_dom_map_without_page_endpoint_fallback(
    monkeypatch,
):
    payload = {
        "id": 99999991,
        "sequential_id": 999991,
        "patient_datum": {"name": "Synthetic Patient"},
        "digital_models": [{
            "id": "synthetic-model",
            "stl_files": [
                {"id": "synthetic-stl-alpha"},
                {"id": "synthetic-stl-beta"},
            ],
        }],
    }

    class ApiSession:
        cookies = {}

        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if "/api/v1/requests/" not in url:
                raise AssertionError("page endpoint must not be consulted")
            if kwargs.get("params", {}).get("access_token") == "synthetic-token":
                return HttpResponse(200, payload)
            return HttpResponse(401, {})

    resolver_calls = []

    def browser_resolver(**kwargs):
        resolver_calls.append(kwargs)
        return {
            "synthetic-stl-alpha": (
                "https://models.example.invalid/alpha"
                "?signature=synthetic-alpha"
            ),
            "synthetic-stl-beta": (
                "https://models.example.invalid/beta"
                "?signature=synthetic-beta"
            ),
        }

    session = ApiSession()
    monkeypatch.setattr(
        CfazProvider,
        "_validate_download_url",
        classmethod(lambda _cls, _url: None),
    )
    provider = CfazProvider(
        api_token="synthetic-token",
        session=session,
        browser_resolver=browser_resolver,
        output=lambda _message: None,
    )

    inventory = provider.discover_digital_models("99999991")

    assert len(inventory.files) == 2
    assert len(resolver_calls) == 1
    assert len(session.calls) == 2
    assert all("/api/v1/requests/" in call[0] for call in session.calls)
