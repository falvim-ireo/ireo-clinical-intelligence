"""Armazenamento local e transacional dos artefatos de exames radiológicos."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import os
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4


class RadiologyStorageError(RuntimeError):
    """Indica uma falha segura de registro ou movimentação de exame."""


class InvalidExamTransitionError(RadiologyStorageError):
    """Indica que a transição solicitada não pertence ao fluxo permitido."""


class ExamState(str, Enum):
    """Estados persistidos de um exame no armazenamento local."""

    INCOMING = "incoming"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    ARCHIVE = "archive"


@dataclass(frozen=True)
class StoredExam:
    """Referência segura aos artefatos persistidos de um exame."""

    exam_id: str
    state: ExamState
    directory: Path
    zip_path: Path
    metadata_path: Path
    metadata: dict[str, Any]


class RadiologyStorage:
    """Registra ZIPs e controla transições por movimentações atômicas."""

    _TRANSITIONS: dict[ExamState, frozenset[ExamState]] = {
        ExamState.INCOMING: frozenset({ExamState.PROCESSING, ExamState.FAILED}),
        ExamState.PROCESSING: frozenset(
            {ExamState.PROCESSED, ExamState.FAILED}
        ),
        ExamState.PROCESSED: frozenset({ExamState.ARCHIVE}),
        ExamState.FAILED: frozenset({ExamState.PROCESSING, ExamState.ARCHIVE}),
        ExamState.ARCHIVE: frozenset(),
    }

    def __init__(
        self,
        root: str | Path,
        *,
        logger: logging.Logger | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        """Cria a raiz e todas as pastas de estado necessárias."""

        self.root = Path(root).expanduser().resolve()
        self.logger = logger or logging.getLogger(__name__)
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self.root.mkdir(parents=True, exist_ok=True)
        for state in ExamState:
            self._state_directory(state).mkdir(parents=False, exist_ok=True)

    def register_received(self, zip_path: str | Path) -> StoredExam:
        """Copia um ZIP recebido para ``incoming`` sem alterar o arquivo de origem."""

        source = Path(zip_path).expanduser().resolve()
        if not source.is_file() or source.suffix.casefold() != ".zip":
            raise RadiologyStorageError("O ZIP recebido não existe ou é inválido.")

        exam_id = self._new_exam_id()
        directory = self._state_directory(ExamState.INCOMING) / exam_id
        try:
            directory.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            raise RadiologyStorageError(
                "Já existe um exame com o identificador gerado."
            ) from None

        destination = directory / source.name
        temporary_zip = directory / f".{source.name}.part"
        now = self._timestamp()
        metadata: dict[str, Any] = {
            "exam_id": exam_id,
            "state": ExamState.INCOMING.value,
            "original_filename": source.name,
            "size_bytes": source.stat().st_size,
            "created_at": now,
            "updated_at": now,
            "history": [
                {"state": ExamState.INCOMING.value, "timestamp": now}
            ],
        }
        try:
            with source.open("rb") as input_file, temporary_zip.open("xb") as output:
                shutil.copyfileobj(input_file, output)
                output.flush()
                os.fsync(output.fileno())
            temporary_zip.replace(destination)
            self._write_metadata_atomic(directory / "metadata.json", metadata)
        except (OSError, ValueError, TypeError):
            shutil.rmtree(directory, ignore_errors=True)
            raise RadiologyStorageError(
                "Não foi possível registrar o ZIP recebido."
            ) from None

        self.logger.info("Exame %s registrado em incoming.", exam_id)
        return self._stored_exam(directory, ExamState.INCOMING, metadata)

    def move(self, exam: StoredExam | str, target_state: ExamState) -> StoredExam:
        """Move um exame para um estado permitido e atualiza seus metadados."""

        if isinstance(exam, StoredExam):
            if target_state not in self._TRANSITIONS[exam.state]:
                raise InvalidExamTransitionError(
                    f"Transição inválida: {exam.state.value} -> {target_state.value}."
                )
            known_destination = self._state_directory(target_state) / exam.exam_id
            if known_destination.exists():
                raise RadiologyStorageError(
                    "O destino do exame já existe; sobrescrita recusada."
                )
        current = self.get(exam.exam_id if isinstance(exam, StoredExam) else exam)
        if target_state not in self._TRANSITIONS[current.state]:
            raise InvalidExamTransitionError(
                f"Transição inválida: {current.state.value} -> {target_state.value}."
            )

        destination = self._state_directory(target_state) / current.exam_id
        if destination.exists():
            raise RadiologyStorageError(
                "O destino do exame já existe; sobrescrita recusada."
            )
        metadata = dict(current.metadata)
        history = list(metadata.get("history", []))
        now = self._timestamp()
        history.append({"state": target_state.value, "timestamp": now})
        metadata.update(
            {
                "state": target_state.value,
                "updated_at": now,
                "history": history,
            }
        )

        try:
            current.directory.replace(destination)
            try:
                self._write_metadata_atomic(destination / "metadata.json", metadata)
            except Exception:
                destination.replace(current.directory)
                raise
        except OSError:
            raise RadiologyStorageError(
                "Não foi possível mover o exame para o novo estado."
            ) from None

        self.logger.info(
            "Exame %s movido de %s para %s.",
            current.exam_id,
            current.state.value,
            target_state.value,
        )
        return self._stored_exam(destination, target_state, metadata)

    def get(self, exam_id: str) -> StoredExam:
        """Localiza um exame pelo identificador sem depender de estado conhecido."""

        normalized_id = str(exam_id or "").strip()
        if not normalized_id or not normalized_id.isalnum():
            raise RadiologyStorageError("Identificador de exame inválido.")
        locations = [
            (state, self._state_directory(state) / normalized_id)
            for state in ExamState
            if (self._state_directory(state) / normalized_id).is_dir()
        ]
        if len(locations) != 1:
            raise RadiologyStorageError(
                "O exame não foi encontrado de forma única."
            )
        state, directory = locations[0]
        metadata_path = directory / "metadata.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            raise RadiologyStorageError(
                "Os metadados do exame não puderam ser lidos."
            ) from None
        if not isinstance(metadata, dict) or metadata.get("state") != state.value:
            raise RadiologyStorageError("Os metadados do exame são inconsistentes.")
        return self._stored_exam(directory, state, metadata)

    def _new_exam_id(self) -> str:
        """Gera um identificador opaco e recusa colisões em qualquer estado."""

        exam_id = str(self._id_factory()).strip()
        if not exam_id or not exam_id.isalnum():
            raise RadiologyStorageError("O gerador produziu um exam_id inválido.")
        if any(
            (self._state_directory(state) / exam_id).exists()
            for state in ExamState
        ):
            raise RadiologyStorageError(
                "Já existe um exame com o identificador gerado."
            )
        return exam_id

    def _stored_exam(
        self,
        directory: Path,
        state: ExamState,
        metadata: dict[str, Any],
    ) -> StoredExam:
        """Reconstrói a referência a partir do diretório e metadata."""

        filename = metadata.get("original_filename")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise RadiologyStorageError("Nome do ZIP inválido nos metadados.")
        return StoredExam(
            exam_id=str(metadata["exam_id"]),
            state=state,
            directory=directory,
            zip_path=directory / filename,
            metadata_path=directory / "metadata.json",
            metadata=metadata,
        )

    def _state_directory(self, state: ExamState) -> Path:
        """Retorna a pasta correspondente a um estado."""

        return self.root / state.value

    @staticmethod
    def _write_metadata_atomic(path: Path, metadata: dict[str, Any]) -> None:
        """Grava JSON completo em arquivo temporário e o substitui atomicamente."""

        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            serialized = json.dumps(
                metadata,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            with temporary.open("x", encoding="utf-8") as output:
                output.write(serialized)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _timestamp() -> str:
        """Retorna timestamp UTC estável para persistência."""

        return datetime.now(timezone.utc).isoformat()
