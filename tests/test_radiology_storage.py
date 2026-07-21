"""Testes do armazenamento local de exames radiológicos."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from storage.radiology_storage import (
    ExamState,
    InvalidExamTransitionError,
    RadiologyStorage,
    RadiologyStorageError,
    StoredExam,
)


def create_zip(path: Path, content: bytes = b"original zip") -> Path:
    """Cria um artefato ZIP opaco suficiente para testar o armazenamento."""

    path.write_bytes(content)
    return path


def test_creates_state_directories_and_registers_incoming_zip(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    source = create_zip(tmp_path / "exam.zip")
    storage = RadiologyStorage(tmp_path / "storage")

    with caplog.at_level(logging.INFO):
        exam = storage.register_received(source)

    assert isinstance(exam, StoredExam)
    assert exam.state is ExamState.INCOMING
    assert exam.exam_id.isalnum()
    assert "exam" not in exam.exam_id.casefold()
    assert source.read_bytes() == b"original zip"
    assert exam.zip_path.read_bytes() == b"original zip"
    assert all((storage.root / state.value).is_dir() for state in ExamState)
    assert "registrado em incoming" in caplog.text


def test_moves_exam_through_valid_states(tmp_path: Path) -> None:
    storage = RadiologyStorage(tmp_path / "storage")
    exam = storage.register_received(create_zip(tmp_path / "exam.zip"))

    processing = storage.move(exam, ExamState.PROCESSING)
    processed = storage.move(processing, ExamState.PROCESSED)
    archived = storage.move(processed, ExamState.ARCHIVE)

    assert processing.state is ExamState.PROCESSING
    assert processed.state is ExamState.PROCESSED
    assert archived.state is ExamState.ARCHIVE
    assert archived.zip_path.read_bytes() == b"original zip"
    assert not exam.directory.exists()
    assert storage.get(exam.exam_id).directory == archived.directory


def test_rejects_invalid_transition(tmp_path: Path) -> None:
    storage = RadiologyStorage(tmp_path / "storage")
    exam = storage.register_received(create_zip(tmp_path / "exam.zip"))

    with pytest.raises(InvalidExamTransitionError, match="Transição inválida"):
        storage.move(exam, ExamState.PROCESSED)

    assert storage.get(exam.exam_id).state is ExamState.INCOMING


def test_failure_state_preserves_original_zip(tmp_path: Path) -> None:
    content = b"zip that must survive"
    storage = RadiologyStorage(tmp_path / "storage")
    exam = storage.register_received(create_zip(tmp_path / "exam.zip", content))
    processing = storage.move(exam, ExamState.PROCESSING)

    failed = storage.move(processing, ExamState.FAILED)

    assert failed.state is ExamState.FAILED
    assert failed.zip_path.read_bytes() == content
    assert failed.metadata["state"] == "failed"


def test_refuses_exam_id_and_destination_collisions(tmp_path: Path) -> None:
    storage = RadiologyStorage(
        tmp_path / "storage",
        id_factory=lambda: "fixedidentifier",
    )
    first = storage.register_received(create_zip(tmp_path / "first.zip"))

    with pytest.raises(RadiologyStorageError, match="Já existe"):
        storage.register_received(create_zip(tmp_path / "second.zip"))

    destination = storage.root / ExamState.PROCESSING.value / first.exam_id
    destination.mkdir()
    marker = destination / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(RadiologyStorageError, match="sobrescrita recusada"):
        storage.move(first, ExamState.PROCESSING)

    assert marker.read_text(encoding="utf-8") == "keep"
    assert first.zip_path.read_bytes() == b"original zip"


def test_metadata_is_created_and_atomically_updated(tmp_path: Path) -> None:
    storage = RadiologyStorage(tmp_path / "storage")
    exam = storage.register_received(create_zip(tmp_path / "exam.zip"))
    initial = json.loads(exam.metadata_path.read_text(encoding="utf-8"))

    processing = storage.move(exam, ExamState.PROCESSING)
    updated = json.loads(processing.metadata_path.read_text(encoding="utf-8"))

    assert initial["exam_id"] == processing.exam_id
    assert initial["state"] == "incoming"
    assert updated["state"] == "processing"
    assert [entry["state"] for entry in updated["history"]] == [
        "incoming",
        "processing",
    ]
    assert updated["created_at"] == initial["created_at"]
    assert updated["updated_at"] >= initial["updated_at"]
    assert list(processing.directory.glob("*.tmp")) == []
    assert list(processing.directory.glob(".metadata.json.*.tmp")) == []
