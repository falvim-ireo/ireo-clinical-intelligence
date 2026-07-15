"""Testes offline da idempotência persistente do intake."""

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from radiology.intake_history import (
    IntakeHistoryError,
    IntakeHistoryRepository,
    _row_to_intake_record,
    fingerprint,
    sanitized_history_lines,
)


def create_record(repository, suffix="1", status="DOWNLOADED", sha="a" * 64):
    record = repository.create_downloaded(
        correlation_id=f"correlation-fixture-{suffix}",
        gmail_message_id=f"gmail-secret-{suffix}",
        transfer_url=f"https://transfernow.net/dl/secret-{suffix}",
        archive_filename="PACIENTE FICTICIO.zip",
        archive_size=123,
        archive_sha256=sha,
    )
    if status != "DOWNLOADED":
        record = repository.update(
            record.id, status,
            patient_id_hash=fingerprint("patient-secret"),
            destination_fingerprint=fingerprint("C:/clinical/secret"),
            file_count=2, total_size=45,
        )
    return record


def intake_row(*, _omit=(), **overrides):
    values = {
        "id": 1,
        "correlation_id": "correlation-fixture",
        "archive_filename_masked": "archive-masked.zip",
        "archive_size": 123,
        "status": "DOWNLOADED",
        "created_at_utc": "2026-07-15T12:00:00Z",
    }
    values.update(overrides)
    for field in _omit:
        values.pop(field)
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    columns = ", ".join(f"? AS {name}" for name in values)
    return connection.execute(columns.join(("SELECT ", "")), tuple(values.values())).fetchone()


def legacy_repository(database, *, version):
    optional_v2 = "" if version == 1 else ", run_id TEXT, reason_code TEXT"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE radiology_imports ("
            "id INTEGER PRIMARY KEY, correlation_id TEXT NOT NULL, "
            "archive_filename_masked TEXT NOT NULL, archive_size INTEGER NOT NULL, "
            "status TEXT NOT NULL, created_at_utc TEXT NOT NULL"
            f"{optional_v2})"
        )
        connection.execute(
            "INSERT INTO radiology_imports "
            "(id, correlation_id, archive_filename_masked, archive_size, status, created_at_utc) "
            "VALUES (1, 'legacy', 'archive-legacy.zip', 1, 'DOWNLOADED', '2026-01-01Z')"
        )
    repository = object.__new__(IntakeHistoryRepository)
    repository.database_path = database
    return repository


def test_missing_database_is_created_with_schema_version(tmp_path):
    database = tmp_path / "nested" / "intake.db"
    assert not database.exists()
    IntakeHistoryRepository(database)
    assert database.is_file()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value FROM schema_info WHERE key='schema_version'"
        ).fetchone()[0] == 2


def test_schema_initialization_is_idempotent(tmp_path):
    database = tmp_path / "intake.db"
    first = IntakeHistoryRepository(database)
    create_record(first)
    second = IntakeHistoryRepository(database)
    assert len(second.list_records()) == 1


def test_get_maps_schema_v1_row(tmp_path):
    record = legacy_repository(tmp_path / "v1.db", version=1).get(1)
    assert record.correlation_id == "legacy"
    assert record.run_id is None
    assert record.reimport_confirmed is False


def test_get_maps_schema_v2_row(tmp_path):
    record = legacy_repository(tmp_path / "v2.db", version=2).get(1)
    assert record.id == 1
    assert record.reason_code is None


def test_mapper_ignores_extra_database_column():
    record = _row_to_intake_record(intake_row(future_schema_column="ignored"))
    assert record.id == 1
    assert not hasattr(record, "future_schema_column")


def test_mapper_defaults_missing_optional_fields():
    record = _row_to_intake_record(intake_row())
    assert record.completed_at_utc is None
    assert record.reimport_confirmed is False


@pytest.mark.parametrize(("stored", "expected"), [(0, False), (1, True)])
def test_mapper_converts_sqlite_booleans(stored, expected):
    assert _row_to_intake_record(
        intake_row(reimport_confirmed=stored)
    ).reimport_confirmed is expected


def test_mapper_preserves_null():
    record = _row_to_intake_record(intake_row(run_id=None, reimport_confirmed=None))
    assert record.run_id is None
    assert record.reimport_confirmed is None


def test_mapper_rejects_invalid_status():
    with pytest.raises(IntakeHistoryError, match="Status inválido"):
        _row_to_intake_record(intake_row(status="UNKNOWN"))


def test_mapper_rejects_missing_required_field_with_sanitized_error():
    with pytest.raises(IntakeHistoryError, match="Registro inválido") as error:
        _row_to_intake_record(intake_row(_omit=("correlation_id",)))
    assert "correlation_id" not in str(error.value)


def test_get_ignores_extra_column_without_type_error(tmp_path):
    database = tmp_path / "extra.db"
    repository = legacy_repository(database, version=2)
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE radiology_imports ADD COLUMN extra TEXT")
        connection.execute("UPDATE radiology_imports SET extra='future'")
    assert repository.get(1).id == 1


def test_find_latest_by_message_reads_legacy_record(tmp_path):
    repository = IntakeHistoryRepository(tmp_path / "intake.db")
    expected = repository.create_downloaded(
        correlation_id="legacy-correlation", archive_filename="safe.zip",
        archive_size=3, archive_sha256="a" * 64,
        gmail_message_id="legacy-message",
    )
    assert repository.find_latest_by_message("legacy-message").id == expected.id


def test_locked_database_preserves_real_sqlite_cause(tmp_path):
    database = tmp_path / "locked.db"
    repository = IntakeHistoryRepository(database)

    def short_timeout_connect():
        connection = sqlite3.connect(database, timeout=0.01)
        connection.row_factory = sqlite3.Row
        return connection

    blocker = sqlite3.connect(database)
    blocker.execute("BEGIN EXCLUSIVE")
    repository._connect = short_timeout_connect
    try:
        with pytest.raises(IntakeHistoryError) as captured:
            repository.create_downloaded(
                correlation_id="locked-correlation", archive_filename="safe.zip",
                archive_size=3, archive_sha256="a" * 64,
            )
    finally:
        blocker.rollback()
        blocker.close()
    assert captured.value.operation == "INSERT_DOWNLOADED"
    assert isinstance(captured.value.__cause__, sqlite3.OperationalError)
    assert captured.value.__cause__.sqlite_errorcode == sqlite3.SQLITE_BUSY


def test_incomplete_v2_schema_migrates_missing_optional_column(tmp_path):
    database = tmp_path / "incomplete.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE radiology_imports ("
            "id INTEGER PRIMARY KEY, correlation_id TEXT NOT NULL, "
            "archive_filename_masked TEXT NOT NULL, archive_size INTEGER NOT NULL, "
            "status TEXT NOT NULL, created_at_utc TEXT NOT NULL)"
        )
    repository = IntakeHistoryRepository(database)
    created = repository.create_downloaded(
        correlation_id="schema-correlation", archive_filename="safe.zip",
        archive_size=3, archive_sha256="a" * 64, gmail_message_id="message",
    )
    assert repository.find_latest_by_message("message").id == created.id


def test_missing_required_schema_column_fails_safely(tmp_path):
    database = tmp_path / "missing-required.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE radiology_imports (id INTEGER PRIMARY KEY, status TEXT)"
        )
    with pytest.raises(IntakeHistoryError) as captured:
        IntakeHistoryRepository(database)
    assert captured.value.operation == "SCHEMA_VALIDATE"
    assert "correlation_id" not in str(captured.value)


def review_candidate(repository, *, correlation, reason="AUTO_COPY_DISABLED",
                     stage="COPY"):
    record = repository.create_downloaded(
        correlation_id=correlation, archive_filename="same.zip",
        archive_size=3, archive_sha256="d" * 64,
        gmail_message_id="same-message",
    )
    record = repository.update(
        record.id, "READY_FOR_CONFIRMATION",
        patient_id_hash="patient-fingerprint",
        destination_fingerprint=f"destination-{correlation}",
    )
    return repository.set_review_required(
        record.id, reason_code=reason, stage=stage, run_id=correlation
    )


def test_equivalent_review_is_updated_without_new_persisted_record(tmp_path):
    repository = IntakeHistoryRepository(tmp_path / "intake.db")
    original, first_skipped = review_candidate(repository, correlation="original-run")
    repeated, second_skipped = review_candidate(repository, correlation="second-run")
    records = repository.list_records(limit=100)
    pending = repository.list_records(status="REVIEW_REQUIRED")
    assert first_skipped is False and second_skipped is True
    assert len(records) == len(pending) == 1
    assert repeated.id == original.id
    assert repeated.correlation_id == "original-run"
    assert repeated.run_id == "second-run"
    assert repeated.updated_at_utc is not None


def test_different_review_reason_creates_separate_record(tmp_path):
    repository = IntakeHistoryRepository(tmp_path / "intake.db")
    review_candidate(repository, correlation="reason-one")
    _, skipped = review_candidate(
        repository, correlation="reason-two", reason="DUPLICATE_POSSIBLE"
    )
    assert skipped is False
    assert len(repository.list_records(status="REVIEW_REQUIRED")) == 2


def test_different_review_stage_creates_separate_record(tmp_path):
    repository = IntakeHistoryRepository(tmp_path / "intake.db")
    review_candidate(repository, correlation="stage-one")
    _, skipped = review_candidate(
        repository, correlation="stage-two", stage="SELECTION"
    )
    assert skipped is False
    assert len(repository.list_records(status="REVIEW_REQUIRED")) == 2


def test_completed_record_is_not_converted_or_merged_with_review(tmp_path):
    repository = IntakeHistoryRepository(tmp_path / "intake.db")
    review_candidate(repository, correlation="pending-run")
    completed = repository.create_downloaded(
        correlation_id="completed-run", archive_filename="same.zip",
        archive_size=3, archive_sha256="d" * 64,
        gmail_message_id="same-message",
    )
    completed = repository.update(
        completed.id, "COMPLETED", patient_id_hash="patient-fingerprint"
    )
    result, skipped = repository.set_review_required(
        completed.id, reason_code="AUTO_COPY_DISABLED", stage="COPY"
    )
    assert skipped is False and result.status == "COMPLETED"
    assert len(repository.list_records(status="REVIEW_REQUIRED")) == 1
    assert len(repository.list_records(status="COMPLETED")) == 1


def test_downloaded_record_and_completed_transition(tmp_path):
    repository = IntakeHistoryRepository(tmp_path / "intake.db")
    record = create_record(repository)
    assert record.status == "DOWNLOADED"
    completed = repository.update(record.id, "COMPLETED", file_count=4, total_size=99)
    assert completed.status == "COMPLETED"
    assert completed.completed_at_utc.endswith("Z")


def test_completed_sha_and_message_are_confirmed_duplicates(tmp_path):
    repository = IntakeHistoryRepository(tmp_path / "intake.db")
    completed = create_record(repository, status="COMPLETED")
    by_sha = repository.find_duplicate(archive_sha256="a" * 64)
    by_message = repository.find_duplicate(gmail_message_id="gmail-secret-1")
    by_transfer = repository.find_duplicate(
        transfer_url="https://transfernow.net/dl/secret-1"
    )
    assert (by_sha.level, by_sha.criterion, by_sha.record.id) == (
        "confirmed", "ARCHIVE_SHA256", completed.id
    )
    assert by_message.criterion == "GMAIL_MESSAGE_ID"
    assert by_transfer.criterion == "TRANSFER_FINGERPRINT"


def test_name_size_patient_without_checksum_is_possible(tmp_path):
    repository = IntakeHistoryRepository(tmp_path / "intake.db")
    create_record(repository, status="COMPLETED", sha=None)
    match = repository.find_duplicate(
        archive_filename="PACIENTE FICTICIO.zip", archive_size=123,
        patient_id="patient-secret",
    )
    assert (match.level, match.criterion) == (
        "possible", "ARCHIVE_NAME_SIZE_PATIENT"
    )


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED"])
def test_failed_and_cancelled_records_allow_retry(tmp_path, status):
    repository = IntakeHistoryRepository(tmp_path / "intake.db")
    create_record(repository, status=status)
    assert repository.find_duplicate(archive_sha256="a" * 64) is None


def test_database_contains_only_fingerprints_and_masked_filename(tmp_path):
    database = tmp_path / "intake.db"
    repository = IntakeHistoryRepository(database)
    create_record(repository, status="COMPLETED")
    raw = database.read_bytes()
    for forbidden in (
        b"PACIENTE FICTICIO", b"patient-secret", b"gmail-secret",
        b"https://transfernow.net", b"C:/clinical/secret",
    ):
        assert forbidden not in raw


def test_history_output_is_sanitized_and_filterable(tmp_path):
    repository = IntakeHistoryRepository(tmp_path / "intake.db")
    record = create_record(repository, status="COMPLETED")
    lines = sanitized_history_lines(repository.list_records(
        status="COMPLETED", correlation_id="correlation-fixture-1"
    ))
    assert len(lines) == 1
    assert record.safe_reference in lines[0]
    assert "PACIENTE FICTICIO" not in lines[0]
    assert "C:/clinical/secret" not in lines[0]


def test_two_simple_concurrent_writes_succeed(tmp_path):
    database = tmp_path / "intake.db"
    repository = IntakeHistoryRepository(database)
    with ThreadPoolExecutor(max_workers=2) as executor:
        records = list(executor.map(
            lambda suffix: create_record(repository, str(suffix), sha=str(suffix) * 64),
            (1, 2),
        ))
    assert len({record.id for record in records}) == 2


def test_failed_update_rolls_back_transaction(tmp_path):
    repository = IntakeHistoryRepository(tmp_path / "intake.db")
    record = create_record(repository)
    with pytest.raises(IntakeHistoryError):
        repository.update(record.id, "COMPLETED", file_count=-1)
    assert repository.get(record.id).status == "DOWNLOADED"


def test_intake_history_cli_prints_only_sanitized_data(tmp_path, monkeypatch, capsys):
    import main
    from core.config import Config

    database = tmp_path / "intake.db"
    repository = IntakeHistoryRepository(database)
    create_record(repository, status="COMPLETED")
    monkeypatch.setattr(Config, "IREO_INTAKE_DATABASE_PATH", str(database))

    assert main.main(["intake-history", "--status", "COMPLETED"]) == 0
    output = capsys.readouterr().out
    assert "COMPLETED" in output
    assert "PACIENTE FICTICIO" not in output
    assert "gmail-secret" not in output
    assert "transfernow.net" not in output
