"""Reparo idempotente de arquivos Cfaz concluídos."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

from acquisition.cfaz_operations import CfazHistoryRepository
from acquisition.cfaz_repair import CfazCompletedImportRepair
from tests.synthetic_fixtures import (
    SYNTHETIC_CLINIC_ID,
    SYNTHETIC_INTERNAL_REQUEST_ID,
    SYNTHETIC_SEQUENTIAL_ID,
)


def jpeg(width: int, height: int, marker: bytes = b"x") -> bytes:
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = (
        b"\xff\xc0\x00\x11\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    )
    return b"\xff\xd8" + app0 + sof + marker + b"\xff\xd9"


class Graph:
    def __init__(self):
        self.renames = []
        self.moves = []
        self.uploads = []

    def find_root_folder(self, path):
        return SimpleNamespace(name=path)

    def list_children(self, folder):
        children = {
            "Pacientes": ["Paciente"],
            "Paciente": ["Radiologia"],
            "Radiologia": ["Exame"],
        }
        return [
            {"id": name, "name": name, "folder": {}}
            for name in children.get(folder.name, [])
        ]

    def folder_from_child_item(self, folder, item):
        return SimpleNamespace(name=item["name"])

    def rename_child_file(self, folder, old, new):
        self.renames.append((folder, old, new))

    def ensure_folder(self, folder, name):
        return SimpleNamespace(name=name)

    def move_child_file(self, folder, old, destination, new):
        self.moves.append((folder.name, old, destination.name, new))

    def upload_small_file(self, folder, path, remote_filename=None):
        self.uploads.append((folder, path.name, remote_filename))


def test_completed_repair_renames_jpeg_updates_manifest_and_is_idempotent(tmp_path):
    database = tmp_path / "index.db"
    history = CfazHistoryRepository(database)
    history.mark_complete(
        request_id=SYNTHETIC_INTERNAL_REQUEST_ID,
        provider_request_id=SYNTHETIC_INTERNAL_REQUEST_ID,
        sequential_id=SYNTHETIC_SEQUENTIAL_ID,
        clinic_number=SYNTHETIC_CLINIC_ID,
        patient_name="Paciente", duration_seconds=1,
        onedrive_destination="Pacientes/Paciente/Radiologia/Exame",
        acquisition_sha="a" * 64,
    )
    staging = tmp_path / "staging" / "Paciente" / "Exame"
    staging.mkdir(parents=True)
    first = jpeg(1200, 800, b"first")
    second = jpeg(270, 270, b"second")
    (staging / "sem-extensao-a").write_bytes(first)
    (staging / "sem-extensao-b").write_bytes(second)
    manifest = {
        "status": "COMPLETED",
        "acquisition": {
            "provider_id": "cfaz",
            "request_id": SYNTHETIC_INTERNAL_REQUEST_ID,
            "provider_request_id": SYNTHETIC_INTERNAL_REQUEST_ID,
            "sequential_id": SYNTHETIC_SEQUENTIAL_ID,
        },
        "checksums": {
            "sem-extensao-a": hashlib.sha256(first).hexdigest(),
            "sem-extensao-b": hashlib.sha256(second).hexdigest(),
        },
        "publication": {
            "state": "COMPLETE",
            "uploaded_files": {
                "sem-extensao-a": {"sha256": hashlib.sha256(first).hexdigest()},
                "sem-extensao-b": {"sha256": hashlib.sha256(second).hexdigest()},
            },
        },
    }
    manifest_path = staging / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), "utf-8")
    graph = Graph()
    repair = CfazCompletedImportRepair(
        history=history, graph=graph, staging_root=tmp_path / "staging",
        now_provider=lambda: datetime(2026, 7, 24, tzinfo=timezone.utc),
    )

    first_result = repair.repair(SYNTHETIC_SEQUENTIAL_ID)

    assert first_result.renamed_files == 2
    clinical_folder = staging / "06 - Documentos"
    assert (clinical_folder / "documentacao_001.jpg").read_bytes() == first
    assert (clinical_folder / "documentacao_002.jpg").read_bytes() == second
    updated = json.loads(manifest_path.read_text("utf-8"))
    assert sorted(updated["checksums"]) == [
        "06 - Documentos/documentacao_001.jpg",
        "06 - Documentos/documentacao_002.jpg",
    ]
    files = updated["acquisition"]["files"]
    assert files[0]["stored_name"] == "documentacao_001.jpg"
    assert files[0]["relative_folder"] == "06 - Documentos"
    assert files[0]["clinical_category"] == "DOCUMENTATION"
    assert files[0]["detected_mime"] == "image/jpeg"
    assert files[0]["sha256"] == hashlib.sha256(first).hexdigest()
    assert files[1]["width"] == 270
    assert updated["status"] == "COMPLETED"
    assert updated["publication"]["state"] == "COMPLETE"
    assert updated["cfaz_file_repair"] == {
        "repaired_at": "2026-07-24T00:00:00Z",
        "repair_version": 16,
        "duplicate_files": 0,
    }
    assert updated["schema_version"] == "16.0"
    record = history.get_record(SYNTHETIC_INTERNAL_REQUEST_ID)
    assert record.status == "COMPLETE"
    assert record.repair_version == 16 and record.repaired_at
    assert len(graph.moves) == 2
    assert graph.uploads[-1][2] == "manifest.json"

    move_count = len(graph.moves)
    upload_count = len(graph.uploads)
    second_result = repair.repair(SYNTHETIC_INTERNAL_REQUEST_ID)
    assert second_result.renamed_files == 0
    assert len(graph.moves) == move_count
    assert len(graph.uploads) == upload_count + 1
