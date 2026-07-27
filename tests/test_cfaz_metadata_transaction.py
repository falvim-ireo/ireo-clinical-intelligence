"""Testes locais da fundação persistente de coordenação CFAZ."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from acquisition.cfaz_metadata_transaction import (
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
                "synthetic-stl-a": {"sha256": "a" * 64},
                "synthetic-stl-b": {"sha256": "b" * 64},
            },
        },
    }
    verify_manifest_operation(prepared_representation, operation_id, models())
    prepared = prepare_manifest(path, prepared_representation)
    published_hash = prepared.publish()
    assert prepared.verify_published(published_hash) == published_hash
    assert prepared.restore(expected_current_sha256=published_hash) == prepared.original_sha256
    assert path.read_bytes() == prepared.original_bytes
