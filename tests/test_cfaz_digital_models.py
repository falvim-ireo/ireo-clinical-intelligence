"""Testes offline do fluxo suplementar de modelos digitais Cfaz."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
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
from acquisition.cfaz_provider import (
    CfazDigitalModelFile,
    CfazDigitalModelInventory,
    CfazProvider,
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

    def discover_digital_models(self, request_id):
        self.discover_calls.append(request_id)
        files = tuple(
            CfazDigitalModelFile(
                digital_model_id="658742",
                stl_file_id=file_id,
                download_url=f"https://storage.googleapis.com/bucket/{file_id}.zip?Signature=secret",
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
    def download_digital_model_archive(self, item, destination):
        self.download_calls.append(item.stl_file_id)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr("../escape.stl", binary_stl(b"unsafe"))
        return sha256(destination.read_bytes()), destination.stat().st_size, "application/zip"


class Graph:
    def __init__(self, manifest):
        self.remote_manifest = deepcopy(manifest)
        self.uploads = []
        self.remote_models = {}

    def find_root_folder(self, path):
        return SimpleNamespace(name=path)

    def list_children(self, folder):
        path_children = {
            "Pacientes": ["Paciente"],
            "Paciente": ["Radiologia"],
            "Radiologia": ["Exame"],
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


class Index:
    def __init__(self):
        self.values = []

    def index_manifest(self, manifest, source):
        self.values.append((deepcopy(manifest), source))


def fixture(tmp_path):
    database = tmp_path / "index.db"
    history = CfazHistoryRepository(database)
    history.mark_complete(
        request_id="26977444",
        provider_request_id="26977444",
        sequential_id="81837",
        clinic_number="4427",
        patient_name="Paciente",
        duration_seconds=1,
        onedrive_destination="Pacientes/Paciente/Radiologia/Exame",
        acquisition_sha="a" * 64,
    )
    staging = tmp_path / "staging" / "Paciente" / "Exame"
    staging.mkdir(parents=True)
    prior = b"prior"
    (staging / "documentacao_001.jpg").write_bytes(prior)
    manifest = {
        "status": "COMPLETED",
        "file_count": 1,
        "total_size_bytes": len(prior),
        "onedrive_destination": "Pacientes/Paciente/Radiologia/Exame",
        "acquisition": {
            "provider_id": "cfaz",
            "request_id": "26977444",
            "provider_request_id": "26977444",
            "sequential_id": "81837",
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
        request_id="26977444",
        provider_request_id="26977444",
        sequential_id="81837",
        clinic_number="4427",
        source_url="https://max.cfaz.net/requests/26977444",
        patient_name="Paciente",
        request_date=None,
        exam_date=None,
        radiology_clinic=None,
        professional=None,
        assets=(),
    )
    provider = Provider(request, {
        "1511267": ("LowerJawScan", binary_stl(b"lower")),
        "1511268": ("UpperJawScan", binary_stl(b"upper")),
    })
    return history, staging, manifest_path, manifest, provider


def supplement(tmp_path, history, provider, graph=None, index=None):
    return CfazDigitalModelSupplement(
        provider=provider,
        history=history,
        graph=graph,
        exam_index_service=index,
        staging_root=tmp_path / "staging",
        max_file_bytes=20 * 1024 * 1024,
        output=lambda _message: None,
        now_provider=lambda: datetime(2026, 7, 24, tzinfo=timezone.utc),
    )


def test_dry_run_enumerates_without_download_or_changes(tmp_path):
    history, _staging, manifest_path, _manifest, provider = fixture(tmp_path)
    before = manifest_path.read_bytes()

    result = supplement(tmp_path, history, provider).run("81837")

    assert result.state == "DRY_RUN"
    assert result.model_count == 1
    assert result.provider_file_count == 2
    assert result.pending_file_count == 2
    assert provider.download_calls == []
    assert manifest_path.read_bytes() == before


def test_cli_exposes_explicit_and_mutually_exclusive_apply(capsys):
    assert main.main(["cfaz-digital-models", "--help"]) == 0
    output = capsys.readouterr().out
    assert "--dry-run" in output
    assert "--apply" in output


def test_apply_adds_two_stl_updates_remote_manifest_and_is_idempotent(tmp_path):
    history, staging, manifest_path, manifest, provider = fixture(tmp_path)
    graph = Graph(manifest)
    index = Index()
    service = supplement(tmp_path, history, provider, graph, index)

    first = service.run("81837", apply=True)

    assert first.state == "COMPLETE"
    assert first.added_file_count == 2
    lower = staging / "04 - Modelos Digitais" / "modelo_mandibula_001.stl"
    upper = staging / "04 - Modelos Digitais" / "modelo_maxila_001.stl"
    assert lower.read_bytes() == binary_stl(b"lower")
    assert upper.read_bytes() == binary_stl(b"upper")
    updated = json.loads(manifest_path.read_text("utf-8"))
    marker = updated["cfaz_digital_models"]
    assert marker["state"] == "COMPLETE"
    assert sorted(marker["provider_files"]) == ["1511267", "1511268"]
    assert all(
        item["remote_uploaded"] for item in marker["provider_files"].values()
    )
    assert updated["publication"]["state"] == "COMPLETE"
    assert updated["publication"]["total_files"] == 3
    assert updated["file_count"] == 3
    package_assets = updated["acquisition"]["clinical_package"]["assets"]
    assert {item["stl_file_id"] for item in package_assets} == {
        "1511267", "1511268",
    }
    serialized = json.dumps(updated)
    assert "storage.googleapis.com" not in serialized
    assert "Signature" not in serialized
    assert history.get_record("81837").status == "COMPLETE"
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

    second = service.run("26977444", apply=True)

    assert second.state == "ALREADY_COMPLETE"
    assert len(provider.download_calls) == download_count
    assert len(graph.uploads) == upload_count


def test_unsafe_zip_is_rejected_before_manifest_or_destination_change(tmp_path):
    history, staging, manifest_path, manifest, provider = fixture(tmp_path)
    provider = UnsafeProvider(provider.request, provider.archives)
    before = manifest_path.read_bytes()

    with pytest.raises(CfazDigitalModelError, match="entrada insegura"):
        supplement(
            tmp_path, history, provider, Graph(manifest), Index()
        ).run("81837", apply=True)

    assert manifest_path.read_bytes() == before
    assert not (staging / "04 - Modelos Digitais").exists()
    assert not (tmp_path / "escape.stl").exists()


def test_failure_before_first_remote_file_rolls_back_local_changes(tmp_path):
    history, staging, manifest_path, manifest, provider = fixture(tmp_path)
    before = manifest_path.read_bytes()

    with pytest.raises(CfazDigitalModelError, match="estado seguro"):
        supplement(
            tmp_path, history, provider, FailingGraph(manifest), Index()
        ).run("81837", apply=True)

    assert manifest_path.read_bytes() == before
    assert not (staging / "04 - Modelos Digitais").exists()


def test_interruption_after_first_upload_resumes_without_duplicate(tmp_path):
    history, _staging, manifest_path, manifest, provider = fixture(tmp_path)
    graph = InterruptedGraph(manifest)
    service = supplement(tmp_path, history, provider, graph, Index())

    with pytest.raises(CfazDigitalModelError, match="estado seguro"):
        service.run("81837", apply=True)

    interrupted = json.loads(manifest_path.read_text("utf-8"))
    assert interrupted["cfaz_digital_models"]["state"] == "IN_PROGRESS"
    assert len(graph.remote_models) == 1
    first_uploads = [
        item for item in graph.uploads if item[1].endswith(".stl")
    ]
    assert len(first_uploads) == 1

    result = service.run("81837", apply=True)

    assert result.state == "COMPLETE"
    assert len(graph.remote_models) == 2
    model_uploads = [
        item for item in graph.uploads if item[1].endswith(".stl")
    ]
    assert len(model_uploads) == 2
    completed = json.loads(manifest_path.read_text("utf-8"))
    assert completed["cfaz_digital_models"]["state"] == "COMPLETE"


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
        if url.endswith("/api/v1/requests/26977444"):
            return HttpResponse(200, self.api_payload)
        if url.endswith("/requests/26977444.json"):
            return HttpResponse(200, self.page_payload)
        raise AssertionError(url)


def test_provider_resolves_two_signed_urls_from_authenticated_page_json():
    api_payload = {
        "id": 26977444,
        "sequential_id": 81837,
        "clinic_id": 4427,
        "patient_datum": {"name": "Paciente"},
        "digital_models": [{
            "id": 658742,
            "model_name": "Escaneamento",
            "stl_files": [
                {"id": 1511267, "digital_model_id": 658742},
                {"id": 1511268, "digital_model_id": 658742},
            ],
        }],
    }
    page_payload = {
        "digital_models": [{
            "id": 658742,
            "stl_files": [
                {
                    "id": 1511267,
                    "name": "LowerJawScan.zip",
                    "download_url": (
                        "https://storage.googleapis.com/bucket/lower.zip"
                        "?GoogleAccessId=x&Expires=1&Signature=secret"
                    ),
                },
                {
                    "id": 1511268,
                    "name": "UpperJawScan.zip",
                    "download_url": (
                        "https://storage.googleapis.com/bucket/upper.zip"
                        "?GoogleAccessId=x&Expires=1&Signature=secret"
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

    inventory = provider.discover_digital_models("26977444")

    assert inventory.model_count == 1
    assert [item.stl_file_id for item in inventory.files] == [
        "1511267", "1511268",
    ]
    assert [item.filename for item in inventory.files] == [
        "LowerJawScan.zip", "UpperJawScan.zip",
    ]
    assert len(session.calls) == 4
    assert all("Signature" not in line for line in output)


def test_provider_uses_configured_session_login_when_page_rejects_api_token():
    api_payload = {
        "id": 26977444,
        "sequential_id": 81837,
        "digital_models": [{
            "id": 658742,
            "stl_files": [{"id": 1511267}],
        }],
    }
    page_payload = {
        "stl_files": [{
            "id": 1511267,
            "download_url": (
                "https://storage.googleapis.com/bucket/lower.zip"
                "?GoogleAccessId=x&Expires=1&Signature=secret"
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
            if url.endswith("/api/v1/requests/26977444"):
                if kwargs.get("params", {}).get("access_token") == "token":
                    return HttpResponse(200, api_payload)
                return HttpResponse(401, {})
            if url.endswith("/requests/26977444.json"):
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

    inventory = provider.discover_digital_models("26977444")

    assert [item.stl_file_id for item in inventory.files] == ["1511267"]
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
