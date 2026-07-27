"""Testes locais da fundação persistente de coordenação CFAZ."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from acquisition.cfaz_metadata_transaction import (
    LocalMetadataCoordinator,
    MetadataTransactionError,
    SupplementModelIdentity,
    SupplementOperationRepository,
    operation_identity,
    prepare_manifest,
    verify_manifest_operation,
)
from acquisition.cfaz_operations import CfazHistoryError, CfazHistoryRepository
from radiology.exam_index_service import ExamIndexError, ExamIndexService
from radiology.intake_history import IntakeHistoryError, IntakeHistoryRepository


def models(*, first_sha="a" * 64, second_sha="b" * 64):
    return [
        SupplementModelIdentity("mandible", "synthetic-stl-a", first_sha, "04/model-a.stl"),
        SupplementModelIdentity("maxilla", "synthetic-stl-b", second_sha, "04/model-b.stl"),
    ]


def manifest(exam_id="synthetic-exam"):
    return {
        "status": "COMPLETED",
        "publication": {"state": "COMPLETE", "exam_id": exam_id},
        "onedrive_destination": "synthetic/destination",
        "acquisition": {"provider_id": "cfaz", "classifications": ["Radiograph"]},
        "assets": [],
    }


def test_operation_identity_is_canonical_and_versioned():
    left_id, left_payload = operation_identity(
        provider="cfaz", request_id="synthetic-request", models=models()
    )
    right_id, right_payload = operation_identity(
        provider="cfaz", request_id="synthetic-request", models=list(reversed(models()))
    )
    changed_id, _ = operation_identity(
        provider="cfaz", request_id="synthetic-request",
        models=models(second_sha="c" * 64),
    )
    assert left_id == right_id and left_payload == right_payload
    assert left_id != changed_id
    assert "https://" not in left_payload
    assert "patient" not in left_payload.casefold()
    assert "token" not in left_payload.casefold()
    assert "cfaz-supplement-operation-v1" in left_payload


def test_operation_registry_migrates_idempotently_and_reloads_phase(tmp_path):
    database = tmp_path / "radiology.db"
    repository = SupplementOperationRepository(str(database))
    operation_id, payload = operation_identity(
        provider="cfaz", request_id="synthetic-request", models=models()
    )
    first = repository.prepare(operation_id, payload)
    second = SupplementOperationRepository(str(database)).prepare(operation_id, payload)
    assert first.phase == second.phase == "PREPARED"
    assert repository.transition(operation_id, "PREPARED", "RADIOLOGY_COMMITTED").phase_version == 1
    assert SupplementOperationRepository(str(database)).get(operation_id).phase == "RADIOLOGY_COMMITTED"
    with pytest.raises(MetadataTransactionError, match="fase esperada"):
        repository.transition(
            operation_id, "RADIOLOGY_COMMITTED", "INTAKE_COMMITTED",
            expected_version=0,
        )
    with pytest.raises(MetadataTransactionError, match="Transição"):
        repository.transition(operation_id, "PREPARED", "COMPLETED")
    with pytest.raises(MetadataTransactionError, match="incompatível"):
        repository.prepare(operation_id, payload.replace("synthetic-request", "other"))


def test_operation_registry_preserves_preexisting_rows_and_rejects_concurrent_payload(
    tmp_path,
):
    database = tmp_path / "radiology.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE legacy_state (id INTEGER PRIMARY KEY, value TEXT)"
        )
        connection.execute("INSERT INTO legacy_state VALUES(1, 'keep')")
    repository = SupplementOperationRepository(str(database))
    operation_id, payload = operation_identity(
        provider="cfaz", request_id="synthetic-request", models=models()
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda _: repository.prepare(operation_id, payload), (1, 2)
        ))
    assert {result.phase for result in results} == {"PREPARED"}
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM legacy_state").fetchone()[0] == "keep"


def test_radiology_history_and_index_share_external_connection_and_rollback(tmp_path):
    database = tmp_path / "radiology.db"
    history = CfazHistoryRepository(database)
    index = ExamIndexService(database)
    history.mark_complete(
        request_id="synthetic-request", patient_name="Synthetic Patient",
        duration_seconds=1, onedrive_destination="synthetic/destination",
    )
    operation_id, payload = operation_identity(
        provider="cfaz", request_id="synthetic-request", models=models()
    )
    registry = SupplementOperationRepository(str(database))
    registry.prepare(operation_id, payload)
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        history.record_supplement(
            request_id="synthetic-request", operation_id=operation_id,
            payload_hash=operation_id, connection=connection,
        )
        index.index_manifest(
            manifest(), source="synthetic-supplement", connection=connection,
            operation_id=operation_id,
        )
        registry.transition(
            operation_id, "PREPARED", "RADIOLOGY_COMMITTED", connection=connection
        )
        connection.rollback()
    assert index.get_by_exam_id("synthetic-exam") is None
    assert history.get_record("synthetic-request").supplement_operation_id is None
    assert registry.get(operation_id).phase == "PREPARED"


def test_external_index_connection_does_not_commit_on_failure(tmp_path):
    database = tmp_path / "radiology.db"
    index = ExamIndexService(database)
    original_consistency = index._record_consistency_issues
    index._record_consistency_issues = lambda *_args: (_ for _ in ()).throw(
        sqlite3.IntegrityError("synthetic failure")
    )
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError):
            index.index_manifest(
                manifest(),
                connection=connection,
            )
        connection.rollback()
    index._record_consistency_issues = original_consistency
    assert index.get_by_exam_id("synthetic-exam") is None


def test_intake_operation_is_unique_idempotent_and_conflict_safe(tmp_path):
    repository = IntakeHistoryRepository(tmp_path / "intake.db")
    first = repository.upsert_supplement(
        operation_id="synthetic-operation", payload_hash="a" * 64,
        correlation_id="synthetic-correlation", archive_sha256="b" * 64,
        file_count=2, total_size=20,
    )
    repeated = repository.upsert_supplement(
        operation_id="synthetic-operation", payload_hash="a" * 64,
        correlation_id="synthetic-correlation", archive_sha256="b" * 64,
        file_count=2, total_size=20,
    )
    assert first.id == repeated.id
    assert len(repository.list_records()) == 1
    with pytest.raises(IntakeHistoryError, match="conflitante"):
        repository.upsert_supplement(
            operation_id="synthetic-operation", payload_hash="c" * 64,
            correlation_id="synthetic-correlation", archive_sha256="b" * 64,
            file_count=2, total_size=20,
        )


def test_manifest_preparation_is_non_mutating_and_detects_optimistic_change(tmp_path):
    path = tmp_path / "manifest.json"
    original = manifest()
    path.write_text(json.dumps(original), "utf-8")
    prepared = prepare_manifest(path, {**original, "prepared": True})
    assert json.loads(path.read_text("utf-8")) == original
    prepared.verify_prepared()
    path.write_text(json.dumps({**original, "concurrent": True}), "utf-8")
    with pytest.raises(MetadataTransactionError, match="alterado"):
        prepared.publish()


def test_manifest_publish_verify_and_restore_use_expected_hashes(tmp_path):
    path = tmp_path / "manifest.json"
    original = manifest()
    path.write_text(json.dumps(original), "utf-8")
    operation_id, _payload = operation_identity(
        provider="cfaz", request_id="synthetic-request", models=models()
    )
    prepared_representation = {
        **original,
        "cfaz_digital_models": {
            "operation_id": operation_id,
            "provider_files": {
                "synthetic-stl-a": {"sha256": "a" * 64, "stl_file_id": "synthetic-stl-a", "destination": "04/model-a.stl"},
                "synthetic-stl-b": {"sha256": "b" * 64, "stl_file_id": "synthetic-stl-b", "destination": "04/model-b.stl"},
            },
        },
    }
    verify_manifest_operation(prepared_representation, operation_id, models())
    prepared = prepare_manifest(path, prepared_representation)
    published_hash = prepared.publish()
    assert prepared.verify_published(published_hash) == published_hash
    assert prepared.restore(expected_current_sha256=published_hash) == prepared.original_sha256
    assert path.read_bytes() == prepared.original_bytes


def coordinator_fixture(tmp_path, *, checkpoint=None, representation_change=None):
    database = tmp_path / "radiology.db"
    intake_db = tmp_path / "intake.db"
    manifest_path = tmp_path / "manifest.json"
    history = CfazHistoryRepository(database)
    index = ExamIndexService(database)
    intake = IntakeHistoryRepository(intake_db)
    operations = SupplementOperationRepository(str(database))
    history.mark_complete(
        request_id="synthetic-request", patient_name="Synthetic Patient",
        duration_seconds=1, onedrive_destination="synthetic/destination",
    )
    operation_id, _ = operation_identity(
        provider="cfaz", request_id="synthetic-request", models=models()
    )
    base = manifest()
    prepared = {
        **base,
        "cfaz_digital_models": {
            "schema_version": 1,
            "state": "COMPLETE",
            "digital_models": 1,
            "stl_files": 2,
            "provider_files": {
                "synthetic-stl-a": {"sha256": "a" * 64, "stl_file_id": "synthetic-stl-a", "relative_path": "04/model-a.stl", "remote_uploaded": True},
                "synthetic-stl-b": {"sha256": "b" * 64, "stl_file_id": "synthetic-stl-b", "relative_path": "04/model-b.stl", "remote_uploaded": True},
            },
        },
    }
    technical_projection = {
        "cfaz_digital_models": {
            "operation_id": operation_id,
            "provider_files": {
                "synthetic-stl-a": {"sha256": "a" * 64, "stl_file_id": "synthetic-stl-a", "destination": "04/model-a.stl"},
                "synthetic-stl-b": {"sha256": "b" * 64, "stl_file_id": "synthetic-stl-b", "destination": "04/model-b.stl"},
            },
        },
    }
    if not manifest_path.exists():
        manifest_path.write_text(json.dumps(base), "utf-8")
    if representation_change:
        prepared = representation_change(prepared)
    coordinator = LocalMetadataCoordinator(
        operations=operations, index=index, history=history, intake=intake,
        manifest_path=manifest_path, provider="cfaz", request_id="synthetic-request",
        models=models(), prepared_representation=prepared,
        technical_projection=technical_projection, checkpoint=checkpoint,
    )
    return coordinator, manifest_path, operations, index, history, intake, prepared


def test_local_coordinator_completes_and_repeats_without_duplicate_effects(tmp_path):
    coordinator, path, operations, index, history, intake, _ = coordinator_fixture(tmp_path)
    first = coordinator.execute()
    second = coordinator.execute()
    assert first.phase == "COMPLETED" and second.idempotent and second.status == "ALREADY_COMPLETE"
    assert operations.get(coordinator.operation_id).phase == "COMPLETED"
    assert index.has_operation(coordinator.operation_id)
    assert history.verify_supplement(
        request_id="synthetic-request", operation_id=coordinator.operation_id,
        payload_hash=coordinator.payload_hash,
    )
    assert intake.get_by_operation(coordinator.operation_id).operation_payload_hash == coordinator.payload_hash
    operational_marker = json.loads(path.read_text("utf-8"))["cfaz_digital_models"]
    assert "operation_id" not in operational_marker
    assert (
        coordinator.technical_projection["cfaz_digital_models"]["operation_id"]
        == coordinator.operation_id
    )


@pytest.mark.parametrize("crash_event", ["RADIOLOGY_COMMITTED", "INTAKE_COMMITTED", "MANIFEST_PUBLISHED"])
def test_local_coordinator_resumes_after_durable_effect_with_new_objects(tmp_path, crash_event):
    def crash(event):
        if event == crash_event:
            raise RuntimeError("synthetic crash")
    coordinator, _path, operations, _index, _history, _intake, prepared = coordinator_fixture(
        tmp_path, checkpoint=crash
    )
    with pytest.raises(RuntimeError, match="synthetic crash"):
        coordinator.execute()
    resumed, _path, operations2, index2, history2, intake2, _ = coordinator_fixture(
        tmp_path
    )
    assert resumed.operation_id == coordinator.operation_id
    result = resumed.execute()
    assert result.phase == "COMPLETED"
    assert operations2.get(resumed.operation_id).phase == "COMPLETED"
    assert index2.has_operation(resumed.operation_id)
    assert history2.verify_supplement(
        request_id="synthetic-request", operation_id=resumed.operation_id,
        payload_hash=resumed.payload_hash,
    )
    assert intake2.get_by_operation(resumed.operation_id) is not None


def test_local_coordinator_routes_intake_conflict_to_review_required(tmp_path):
    coordinator, _path, operations, _index, _history, _intake, _ = coordinator_fixture(tmp_path)
    IntakeHistoryRepository(tmp_path / "intake.db").upsert_supplement(
        operation_id=coordinator.operation_id, payload_hash="f" * 64,
        correlation_id="synthetic-correlation", archive_sha256=None,
        file_count=2, total_size=0,
    )
    with pytest.raises(MetadataTransactionError, match="revisão"):
        coordinator.execute()
    assert operations.get(coordinator.operation_id).phase == "REVIEW_REQUIRED"


def test_local_coordinator_rejects_manifest_changed_between_phases(tmp_path):
    changed = {"changed": True}
    def checkpoint(event):
        if event == "INTAKE_COMMITTED":
            (tmp_path / "manifest.json").write_text(json.dumps(changed), "utf-8")
    coordinator, _path, operations, _index, _history, _intake, _ = coordinator_fixture(
        tmp_path, checkpoint=checkpoint
    )
    with pytest.raises(MetadataTransactionError, match="Manifesto"):
        coordinator.execute()
    assert operations.get(coordinator.operation_id).phase == "REVIEW_REQUIRED"


def test_local_coordinator_two_workers_produce_one_operation(tmp_path):
    first, _path, _operations, _index, _history, _intake, prepared = coordinator_fixture(tmp_path)
    second, _path, _operations, _index, _history, _intake, prepared = coordinator_fixture(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda item: item.execute(), (first, second)))
    assert {result.phase for result in results} == {"COMPLETED"}
    with sqlite3.connect(tmp_path / "radiology.db") as db:
        assert db.execute("SELECT COUNT(*) FROM cfaz_supplement_operations").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM import_history WHERE operation_id IS NOT NULL").fetchone()[0] == 1


def test_invalid_preparation_has_no_persistent_effects(tmp_path):
    def incompatible(value):
        changed = json.loads(json.dumps(value))
        changed["cfaz_digital_models"]["provider_files"]["synthetic-stl-a"]["sha256"] = "f" * 64
        return changed
    coordinator, _path, operations, index, history, intake, _ = coordinator_fixture(
        tmp_path, representation_change=incompatible
    )
    with pytest.raises(MetadataTransactionError):
        coordinator.execute()
    assert operations.get(coordinator.operation_id) is None
    assert not index.has_operation(coordinator.operation_id)
    assert history.verify_supplement(
        request_id="synthetic-request", operation_id=coordinator.operation_id,
        payload_hash=coordinator.payload_hash,
    ) is False
    assert intake.get_by_operation(coordinator.operation_id) is None


def test_preparation_anchor_is_durable_and_external_change_requires_review(tmp_path):
    coordinator, path, operations, _index, _history, _intake, _ = coordinator_fixture(tmp_path)
    with pytest.raises(RuntimeError):
        coordinator.checkpoint = lambda event: (_ for _ in ()).throw(RuntimeError()) if event == "RADIOLOGY_COMMITTED" else None
        coordinator.execute()
    row = operations.get(coordinator.operation_id)
    assert row.preparation_version == 1
    assert row.manifest_original_sha256 and row.manifest_prepared_sha256 and row.prepared_delta_json
    path.write_text(json.dumps({"external": True}), "utf-8")
    resumed, *_ = coordinator_fixture(tmp_path)
    with pytest.raises(MetadataTransactionError, match="revisão"):
        resumed.resume()
    assert operations.get(coordinator.operation_id).phase == "REVIEW_REQUIRED"


@pytest.mark.parametrize("mutation", ["missing", "fingerprint", "operation"])
def test_resume_rejects_missing_or_incompatible_technical_projection(
    tmp_path, mutation,
):
    coordinator, _path, operations, _index, _history, _intake, _ = (
        coordinator_fixture(tmp_path)
    )
    coordinator.checkpoint = (
        lambda event: (_ for _ in ()).throw(RuntimeError())
        if event == "RADIOLOGY_COMMITTED" else None
    )
    with pytest.raises(RuntimeError):
        coordinator.execute()
    resumed, *_ = coordinator_fixture(tmp_path)
    marker = resumed.technical_projection["cfaz_digital_models"]
    if mutation == "missing":
        marker["provider_files"].pop("synthetic-stl-a")
    elif mutation == "fingerprint":
        marker["provider_files"]["synthetic-stl-a"]["sha256"] = "f" * 64
    else:
        marker["operation_id"] = "wrong-operation"

    with pytest.raises(MetadataTransactionError):
        resumed.resume()

    assert operations.get(coordinator.operation_id).phase == "REVIEW_REQUIRED"


def test_destination_claims_are_persistent_and_conflict_safe(tmp_path):
    first, _path, operations, _index, _history, _intake, _ = coordinator_fixture(tmp_path)
    with pytest.raises(RuntimeError):
        first.checkpoint = lambda event: (_ for _ in ()).throw(RuntimeError()) if event == "RADIOLOGY_COMMITTED" else None
        first.execute()
    assert len(operations.destination_claims(first.operation_id)) == 2
    second, _path, _operations, _index, _history, _intake, _ = coordinator_fixture(tmp_path)
    second.request_id = "other-request"
    second.operation_id, second.payload_json = operation_identity(
        provider="cfaz", request_id="other-request", models=second.models
    )
    second.payload_hash = hashlib.sha256(
        second.payload_json.encode("utf-8")
    ).hexdigest()
    second.technical_projection["cfaz_digital_models"][
        "operation_id"
    ] = second.operation_id
    with pytest.raises(MetadataTransactionError, match="Destino"):
        second.execute()


def test_index_failure_rolls_back_history_and_phase(tmp_path, monkeypatch):
    coordinator, _path, operations, index, history, _intake, _ = coordinator_fixture(tmp_path)
    original = index.index_manifest
    monkeypatch.setattr(index, "index_manifest", lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.IntegrityError("synthetic index failure")))
    with pytest.raises(sqlite3.IntegrityError):
        coordinator.execute()
    assert operations.get(coordinator.operation_id).phase == "PREPARED"
    assert not index.has_operation(coordinator.operation_id)
    assert history.verify_supplement(request_id="synthetic-request", operation_id=coordinator.operation_id, payload_hash=coordinator.payload_hash) is False
    monkeypatch.setattr(index, "index_manifest", original)


def test_history_failure_rolls_back_index_and_phase(tmp_path, monkeypatch):
    coordinator, _path, operations, index, history, _intake, _ = coordinator_fixture(tmp_path)
    monkeypatch.setattr(history, "record_supplement", lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.IntegrityError("synthetic history failure")))
    with pytest.raises(sqlite3.IntegrityError):
        coordinator.execute()
    assert operations.get(coordinator.operation_id).phase == "PREPARED"
    assert not index.has_operation(coordinator.operation_id)
    assert history.verify_supplement(request_id="synthetic-request", operation_id=coordinator.operation_id, payload_hash=coordinator.payload_hash) is False


def test_transition_failure_rolls_back_index_and_history(tmp_path, monkeypatch):
    coordinator, _path, operations, index, history, _intake, _ = coordinator_fixture(tmp_path)
    original = operations.transition
    def fail_once(operation_id, expected_phase, new_phase, **kwargs):
        if expected_phase == "PREPARED" and kwargs.get("connection") is not None:
            raise MetadataTransactionError("synthetic transition failure")
        return original(operation_id, expected_phase, new_phase, **kwargs)
    monkeypatch.setattr(operations, "transition", fail_once)
    with pytest.raises(MetadataTransactionError, match="synthetic transition"):
        coordinator.execute()
    assert operations.get(coordinator.operation_id).phase == "PREPARED"
    assert not index.has_operation(coordinator.operation_id)
    assert history.verify_supplement(request_id="synthetic-request", operation_id=coordinator.operation_id, payload_hash=coordinator.payload_hash) is False


def test_completed_postcondition_violation_enters_review(tmp_path):
    coordinator, _path, operations, _index, _history, _intake, _ = coordinator_fixture(tmp_path)
    assert coordinator.execute().phase == "COMPLETED"
    with sqlite3.connect(tmp_path / "radiology.db") as db:
        db.execute("DELETE FROM import_history WHERE operation_id=?", (coordinator.operation_id,))
    with pytest.raises(MetadataTransactionError, match="índice"):
        coordinator.execute()
    assert operations.get(coordinator.operation_id).phase == "REVIEW_REQUIRED"


def test_resume_is_explicit_public_contract(tmp_path):
    coordinator, _path, _operations, _index, _history, _intake, _ = coordinator_fixture(tmp_path)
    assert callable(coordinator.resume)
    assert coordinator.resume.__name__ == "resume"


def test_index_projection_schema_migrates_idempotently(tmp_path):
    database = tmp_path / "radiology.db"
    ExamIndexService(database)
    ExamIndexService(database)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cfaz_supplement_index_projection'").fetchone() is not None
        columns = {row[1] for row in db.execute("PRAGMA table_info(cfaz_supplement_index_projection)")}
    assert {"operation_id", "semantic_role", "stl_file_id", "asset_sha256", "destination_logical"} <= columns


@pytest.mark.parametrize("mutation", [
    "role", "swap", "stl_file_id", "fingerprint", "destination", "missing", "additional",
])
def test_completed_index_projection_rejects_each_content_corruption(tmp_path, mutation):
    coordinator, _path, operations, _index, _history, _intake, _ = coordinator_fixture(tmp_path)
    assert coordinator.execute().status == "COMPLETED"
    with sqlite3.connect(tmp_path / "radiology.db") as db:
        if mutation == "role":
            role = db.execute("SELECT semantic_role FROM cfaz_supplement_index_projection WHERE operation_id=? ORDER BY semantic_role LIMIT 1", (coordinator.operation_id,)).fetchone()[0]
            db.execute("UPDATE cfaz_supplement_index_projection SET semantic_role='temporary' WHERE operation_id=? AND semantic_role=?", (coordinator.operation_id, role))
            db.execute("UPDATE cfaz_supplement_index_projection SET semantic_role='wrong' WHERE operation_id=? AND semantic_role='temporary'", (coordinator.operation_id,))
        elif mutation == "swap":
            rows = db.execute("SELECT semantic_role,stl_file_id FROM cfaz_supplement_index_projection WHERE operation_id=? ORDER BY semantic_role", (coordinator.operation_id,)).fetchall()
            db.execute("UPDATE cfaz_supplement_index_projection SET semantic_role='temporary' WHERE operation_id=? AND semantic_role=?", (coordinator.operation_id, rows[0][0]))
            db.execute("UPDATE cfaz_supplement_index_projection SET semantic_role=? WHERE operation_id=? AND semantic_role=?", (rows[0][0], coordinator.operation_id, rows[1][0]))
            db.execute("UPDATE cfaz_supplement_index_projection SET semantic_role=? WHERE operation_id=? AND semantic_role='temporary'", (rows[1][0], coordinator.operation_id))
        elif mutation == "stl_file_id":
            stl = db.execute("SELECT stl_file_id FROM cfaz_supplement_index_projection WHERE operation_id=? ORDER BY stl_file_id LIMIT 1", (coordinator.operation_id,)).fetchone()[0]
            db.execute("UPDATE cfaz_supplement_index_projection SET stl_file_id='temporary' WHERE operation_id=? AND stl_file_id=?", (coordinator.operation_id, stl))
            db.execute("UPDATE cfaz_supplement_index_projection SET stl_file_id='wrong' WHERE operation_id=? AND stl_file_id='temporary'", (coordinator.operation_id,))
        elif mutation == "fingerprint":
            db.execute("UPDATE cfaz_supplement_index_projection SET asset_sha256=? WHERE operation_id=?", ("f" * 64, coordinator.operation_id))
        elif mutation == "destination":
            db.execute("UPDATE cfaz_supplement_index_projection SET destination_logical='wrong/model.stl' WHERE operation_id=?", (coordinator.operation_id,))
        elif mutation == "missing":
            role = db.execute("SELECT semantic_role FROM cfaz_supplement_index_projection WHERE operation_id=? ORDER BY semantic_role LIMIT 1", (coordinator.operation_id,)).fetchone()[0]
            db.execute("DELETE FROM cfaz_supplement_index_projection WHERE operation_id=? AND semantic_role=?", (coordinator.operation_id, role))
        else:
            db.execute("INSERT INTO cfaz_supplement_index_projection VALUES(?,?,?,?,?,?,?,?)", (coordinator.operation_id, coordinator.payload_hash, coordinator.request_id, "extra", "extra", "e" * 64, "extra.stl", "x" * 64))
    with pytest.raises(MetadataTransactionError, match="índice"):
        coordinator.execute()
    assert operations.get(coordinator.operation_id).phase == "REVIEW_REQUIRED"


def test_completed_history_adulteration_enters_review(tmp_path):
    coordinator, _path, operations, _index, _history, _intake, _ = coordinator_fixture(tmp_path)
    assert coordinator.execute().status == "COMPLETED"
    with sqlite3.connect(tmp_path / "radiology.db") as db:
        db.execute("UPDATE cfaz_import_history SET supplement_payload_hash=? WHERE request_id=?", ("f" * 64, coordinator.request_id))
    with pytest.raises(MetadataTransactionError, match="histórico"):
        coordinator.execute()
    assert operations.get(coordinator.operation_id).phase == "REVIEW_REQUIRED"


def test_completed_intake_adulteration_enters_review(tmp_path):
    coordinator, _path, operations, _index, _history, _intake, _ = coordinator_fixture(tmp_path)
    assert coordinator.execute().status == "COMPLETED"
    with sqlite3.connect(tmp_path / "intake.db") as db:
        db.execute("UPDATE radiology_imports SET operation_payload_hash=? WHERE operation_id=?", ("f" * 64, coordinator.operation_id))
    with pytest.raises(MetadataTransactionError, match="intake"):
        coordinator.execute()
    assert operations.get(coordinator.operation_id).phase == "REVIEW_REQUIRED"


def test_completed_manifest_adulteration_enters_review(tmp_path):
    coordinator, path, operations, _index, _history, _intake, _ = coordinator_fixture(tmp_path)
    assert coordinator.execute().status == "COMPLETED"
    path.write_text(json.dumps({"external": True}), "utf-8")
    with pytest.raises(MetadataTransactionError, match="revisão"):
        coordinator.execute()
    assert operations.get(coordinator.operation_id).phase == "REVIEW_REQUIRED"


def test_completed_claim_adulteration_enters_review_and_stays_fail_closed(tmp_path):
    coordinator, _path, operations, _index, _history, _intake, _ = coordinator_fixture(tmp_path)
    assert coordinator.execute().status == "COMPLETED"
    with sqlite3.connect(tmp_path / "radiology.db") as db:
        db.execute("DELETE FROM cfaz_supplement_destination_claims WHERE operation_id=?", (coordinator.operation_id,))
    with pytest.raises(MetadataTransactionError, match="Claims"):
        coordinator.execute()
    assert operations.get(coordinator.operation_id).phase == "REVIEW_REQUIRED"
    with pytest.raises(MetadataTransactionError, match="Claims"):
        coordinator.execute()


def test_completed_anchor_adulteration_enters_review(tmp_path):
    coordinator, _path, operations, _index, _history, _intake, _ = coordinator_fixture(tmp_path)
    assert coordinator.execute().status == "COMPLETED"
    with sqlite3.connect(tmp_path / "radiology.db") as db:
        db.execute("UPDATE cfaz_supplement_operations SET prepared_delta_json=? WHERE operation_id=?", ("{}", coordinator.operation_id))
    with pytest.raises(MetadataTransactionError, match="revisão"):
        coordinator.execute()
    assert operations.get(coordinator.operation_id).phase == "REVIEW_REQUIRED"


def test_completed_payload_conflict_is_rejected_without_mutation(tmp_path):
    coordinator, _path, operations, _index, _history, _intake, _ = coordinator_fixture(tmp_path)
    assert coordinator.execute().status == "COMPLETED"
    before = operations.get(coordinator.operation_id)
    conflicting = coordinator.payload_json.replace("synthetic-stl-a", "other-stl-a")
    with pytest.raises(MetadataTransactionError, match="incompatível"):
        operations.prepare(coordinator.operation_id, conflicting)
    after = operations.get(coordinator.operation_id)
    assert after.phase == before.phase == "COMPLETED"
    assert after.phase_version == before.phase_version
    assert after.prepared_delta_json == before.prepared_delta_json
