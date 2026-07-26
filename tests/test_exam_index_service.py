"""Cobertura do índice radiológico derivado e do rebuild somente leitura."""

from __future__ import annotations

import sqlite3
import time

from integrations.onedrive_graph import GraphFolder, OneDriveGraphError
from radiology.exam_index_service import ExamIndexService, OneDriveRadiologyIndexRebuilder
from synthetic_fixtures import (
    SYNTHETIC_PATIENT_ALPHA,
    SYNTHETIC_PATIENT_ALPHA_ASCII,
)


def manifest(
    exam_id: str,
    *,
    patient: str = SYNTHETIC_PATIENT_ALPHA,
    exam_date: str = "2026-03-10",
    exam_time: str = "11:10:13",
    modality: str = "CT",
    manufacturer: str = "Carestream",
    study_uid: str = "1.2.3",
    series_uid: str = "1.2.3.4",
    state: str = "COMPLETE",
) -> dict:
    return {
        "exam_date": exam_date,
        "exam_time": exam_time,
        "exam_date_source": "DICOM_STUDY_DATE",
        "masked_patient_id": "patient-abc",
        "onedrive_destination": f"Pacientes/{patient}/Radiologia/{exam_date} - Radiologia",
        "import_started_at": f"{exam_date}T11:11:00Z",
        "import_completed_at": f"{exam_date}T11:12:00Z",
        "publication": {
            "exam_id": exam_id,
            "source_archive_sha256": exam_id,
            "state": state,
        },
        "assets": [{
            "source_collection": "request.teleradiographies[1]",
            "stored_name": "telerradiografia_001.jpg",
            "relative_folder": "01 - Radiografias",
            "detected_mime": "image/jpeg",
            "detected_extension": ".jpg",
            "asset_type": "IMAGE",
            "clinical_category": "RADIOGRAPH",
            "width": 1200, "height": 800, "size_bytes": 1234,
            "sha256": "f" * 64, "is_thumbnail": False,
            "is_duplicate": False, "duplicate_of": None,
            "normalized_at": "2026-07-24T00:00:00Z",
        }],
        "dicom_intelligence": {
            "patient_count": 1,
            "alerts": [],
            "studies": [{
                "study_instance_uid": study_uid,
                "study_date": exam_date.replace("-", ""),
                "study_time": exam_time.replace(":", ""),
                "study_description": "Aquisição estrutural",
                "modality": modality,
                "manufacturer": manufacturer,
                "manufacturer_model_name": "CS 9600",
                "software_versions": "1.0",
                "institution_name": "IREO",
                "series": [{
                    "series_instance_uid": series_uid,
                    "modality": modality,
                    "series_description": "Volume",
                    "image_count": 501,
                    "rows": 400,
                    "columns": 500,
                    "estimated_voxel_size": {
                        "value": [0.2, 0.2, 0.2], "unit": "mm",
                        "estimated": True, "sources": ["PixelSpacing", "SliceThickness"],
                    },
                    "estimated_fov": {
                        "value": [80.0, 100.0, 100.2], "unit": "mm",
                        "estimated": True, "sources": ["Rows", "Columns", "PixelSpacing"],
                    },
                }],
            }],
        },
    }


def test_indexes_persists_and_queries_all_supported_keys(tmp_path) -> None:
    database = tmp_path / "radiology.db"
    service = ExamIndexService(database)
    service.index_manifest(manifest("a" * 64), patient_name=SYNTHETIC_PATIENT_ALPHA)

    reopened = ExamIndexService(database)
    assert reopened.get_by_exam_id("a" * 64).series_count == 1
    assert reopened.find_by_patient(SYNTHETIC_PATIENT_ALPHA_ASCII)[0].exam_id == "a" * 64
    assert reopened.find_by_date("2026-03-10")
    assert reopened.find_by_modality("ct")
    assert reopened.find_by_manufacturer("carestream")
    assert reopened.find_by_study_uid("1.2.3")
    assert reopened.find_by_series_uid("1.2.3.4")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM exams").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM studies").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM series").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM exam_assets").fetchone()[0] == 1
        assert db.execute(
            "SELECT clinical_category FROM exam_assets"
        ).fetchone()[0] == "RADIOGRAPH"


def test_incremental_update_timeline_comparison_and_dashboard(tmp_path) -> None:
    service = ExamIndexService(tmp_path / "index.db")
    first = manifest("a" * 64)
    second = manifest(
        "b" * 64, exam_date="2026-06-20", exam_time="09:00:00",
        modality="DX", manufacturer="Planmeca", study_uid="2.3.4", series_uid="2.3.4.5",
    )
    service.index_manifest(second)
    service.index_manifest(first)
    updated = manifest("a" * 64, manufacturer="Carestream Dental")
    service.index_manifest(updated)

    timeline = service.timeline(SYNTHETIC_PATIENT_ALPHA_ASCII)
    assert [item.exam_id for item in timeline] == ["a" * 64, "b" * 64]
    assert service.get_by_exam_id("a" * 64).manufacturer == "Carestream Dental"
    assert service.compare("a" * 64, "b" * 64).differences["modality"] == ("CT", "DX")
    dashboard = service.dashboard()
    assert dashboard["patients"] == 1
    assert dashboard["exams"] == 2
    assert dashboard["index_version"] == 1


def test_consistency_checks_duplicate_study_dates_patient_and_missing_series(tmp_path) -> None:
    service = ExamIndexService(tmp_path / "index.db")
    service.index_manifest(manifest("a" * 64, study_uid="shared"))
    inconsistent = manifest("b" * 64, exam_date="2026-04-01", study_uid="shared")
    intelligence = inconsistent["dicom_intelligence"]
    intelligence["patient_count"] = 2
    intelligence["alerts"] = ["UIDs duplicados", "nome incompatível"]
    intelligence["studies"][0]["study_date"] = "20260310"
    intelligence["studies"][0]["series"] = []
    service.index_manifest(inconsistent)

    codes = {item["code"] for item in service.consistency_issues("b" * 64)}
    assert {"DUPLICATE_STUDY", "DUPLICATE_UID", "INCOMPATIBLE_PATIENT",
            "INCONSISTENT_DATES", "MISSING_SERIES"}.issubset(codes)


class FakeGraph:
    def __init__(self, manifests):
        self.root = GraphFolder("root", "Pacientes")
        self.manifests = manifests
        self.fail_once: set[str] = set()
        self.downloaded: list[str] = []
        self.list_calls: list[str] = []

    def list_children(self, folder):
        self.list_calls.append(folder.item_id)
        if folder.item_id == "root":
            names = sorted({patient for patient, _ in self.manifests})
            return [{"id": f"p:{name}", "name": name, "folder": {}} for name in names]
        if folder.item_id.startswith("p:"):
            return [{"id": f"r:{folder.name}", "name": "Radiologia", "folder": {}}]
        if folder.item_id.startswith("r:"):
            patient_name = folder.item_id.split(":", 1)[1]
            return [
                {"id": f"e:{patient_name}:{exam}", "name": exam, "folder": {}}
                for patient, exam in self.manifests if patient == patient_name
            ]
        if folder.item_id.startswith("e:"):
            return [
                {"id": f"manifest:{folder.item_id}", "name": "manifest.json", "file": {}}
            ]
        return []

    def find_root_folder(self, name): return self.root

    def find_child_folder(self, parent, name):
        for item in self.list_children(parent):
            if item["name"] == name:
                if parent.item_id == "root":
                    return GraphFolder(item["id"], name)
                if parent.item_id.startswith("p:"):
                    return GraphFolder(item["id"], parent.name)
                return GraphFolder(item["id"], name)
        return None

    def download_json_file(self, folder, filename):
        key = (folder.item_id.split(":", 2)[1], folder.name)
        self.downloaded.append(folder.item_id)
        if folder.item_id in self.fail_once:
            self.fail_once.remove(folder.item_id)
            raise RuntimeError("interrupted")
        return self.manifests[key]


def test_rebuild_full_partial_resume_idempotency_and_index_version(tmp_path) -> None:
    manifests = {
        ("Paciente A", "2026-01-01 - Radiologia"): manifest("a" * 64, patient="Paciente A"),
        ("Paciente B", "2026-02-01 - Radiologia"): manifest("b" * 64, patient="Paciente B"),
    }
    graph = FakeGraph(manifests)
    service = ExamIndexService(tmp_path / "index.db")
    rebuilder = OneDriveRadiologyIndexRebuilder(
        graph=graph, onedrive_root="Pacientes", index=service
    )

    partial = rebuilder.rebuild(patient_name="Paciente A")
    assert (partial.discovered, partial.indexed) == (1, 1)
    repeated = rebuilder.rebuild(patient_name="Paciente A")
    assert (repeated.indexed, repeated.skipped) == (0, 1)

    failing_id = "e:Paciente B:2026-02-01 - Radiologia"
    graph.fail_once.add(failing_id)
    interrupted = rebuilder.rebuild()
    assert interrupted.failed == 1
    resumed = rebuilder.rebuild()
    assert resumed.indexed == 1
    assert service.dashboard()["exams"] == 2

    upgraded = ExamIndexService(tmp_path / "index.db", index_version=2)
    upgraded_result = OneDriveRadiologyIndexRebuilder(
        graph=graph, onedrive_root="Pacientes", index=upgraded
    ).rebuild()
    assert upgraded_result.indexed == 2
    assert upgraded.get_by_exam_id("a" * 64).index_version == 2

    full = OneDriveRadiologyIndexRebuilder(
        graph=graph, onedrive_root="Pacientes", index=upgraded
    ).rebuild(full=True)
    assert full.indexed == 2
    assert upgraded.dashboard()["exams"] == 2


def test_administrative_rebuild_command_is_read_only_and_reports_dashboard(
    tmp_path, monkeypatch, capsys,
) -> None:
    import main
    from core.config import Config

    graph = FakeGraph({
        ("Paciente A", "2026-01-01 - Radiologia"): manifest(
            "a" * 64, patient="Paciente A"
        )
    })
    monkeypatch.setattr(main, "build_onedrive_graph_client", lambda: graph)
    monkeypatch.setattr(Config, "MS_GRAPH_ONEDRIVE_ROOT", "Pacientes")
    monkeypatch.setattr(
        Config, "IREO_RADIOLOGY_INDEX_DATABASE_PATH", str(tmp_path / "command.db")
    )

    assert main.main(["rebuild-radiology-index"]) == 0
    output = capsys.readouterr().out
    assert "indexados=1" in output
    assert "pacientes=1, exames=1" in output
    assert output.index("Inicializando rebuild...") < output.index(
        "Conectando ao Microsoft Graph..."
    )
    assert "Localizando pasta raiz... OK" in output
    assert "Tempos:" in output
    assert not hasattr(graph, "upload_small_file")


def test_inventory_lists_each_folder_once_and_reports_patient_progress(tmp_path) -> None:
    graph = FakeGraph({
        ("Paciente A", "Exame A"): manifest("a" * 64, patient="Paciente A"),
        ("Paciente B", "Exame B"): manifest("b" * 64, patient="Paciente B"),
    })
    output = []
    result = OneDriveRadiologyIndexRebuilder(
        graph=graph, onedrive_root="Pacientes",
        index=ExamIndexService(tmp_path / "index.db"), output=output.append,
        retry_delay_seconds=0,
    ).rebuild()

    assert result.indexed == 2
    assert graph.list_calls == [
        "root", "p:Paciente A", "r:Paciente A", "e:Paciente A:Exame A",
        "p:Paciente B", "r:Paciente B", "e:Paciente B:Exame B",
    ]
    assert len(graph.list_calls) == len(set(graph.list_calls))
    assert "Pacientes encontrados: 2" in output
    assert "Paciente 1/2" in output and "Paciente 2/2" in output
    assert any(line.startswith("Etapas: inventário=") for line in output)


def test_slow_graph_call_emits_heartbeat_and_transient_failure_retries(tmp_path) -> None:
    class SlowTransientGraph(FakeGraph):
        failures = 1

        def find_root_folder(self, name):
            time.sleep(0.03)
            if self.failures:
                self.failures -= 1
                raise OneDriveGraphError("timeout transitório")
            return self.root

    output = []
    result = OneDriveRadiologyIndexRebuilder(
        graph=SlowTransientGraph({}), onedrive_root="Pacientes",
        index=ExamIndexService(tmp_path / "index.db"), output=output.append,
        heartbeat_seconds=0.01, max_attempts=5, retry_delay_seconds=0,
    ).rebuild()

    assert result.discovered == 0
    assert sum("Microsoft Graph demorando" in line for line in output) >= 2
    assert "Tentativa 2 de 5..." in output
