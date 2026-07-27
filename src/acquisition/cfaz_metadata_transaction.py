"""Primitivas persistentes para futuras transações de suplemento CFAZ."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Iterable


ALGORITHM_VERSION = 1
CAPABILITY = "cfaz-supplement-metadata-foundation-v1"
PHASES = (
    "PREPARED", "RADIOLOGY_COMMITTED", "INTAKE_COMMITTED",
    "MANIFEST_PUBLISHED", "COMPLETED", "COMPENSATING", "REVIEW_REQUIRED",
)
_TRANSITIONS = {
    "PREPARED": {"RADIOLOGY_COMMITTED", "COMPENSATING", "REVIEW_REQUIRED"},
    "RADIOLOGY_COMMITTED": {"INTAKE_COMMITTED", "COMPENSATING", "REVIEW_REQUIRED"},
    "INTAKE_COMMITTED": {"MANIFEST_PUBLISHED", "COMPENSATING", "REVIEW_REQUIRED"},
    "MANIFEST_PUBLISHED": {"COMPLETED", "COMPENSATING", "REVIEW_REQUIRED"},
    "COMPENSATING": {"REVIEW_REQUIRED", "PREPARED"},
    "REVIEW_REQUIRED": {"PREPARED"},
    "COMPLETED": set(),
}


class MetadataTransactionError(RuntimeError):
    """Falha sanitizada nas primitivas persistentes de metadados."""


@dataclass(frozen=True)
class SupplementModelIdentity:
    semantic_role: str
    stl_file_id: str
    sha256: str
    destination: str


@dataclass(frozen=True)
class SupplementOperation:
    operation_id: str
    algorithm_version: int
    provider: str
    request_id: str
    payload_json: str
    phase: str
    phase_version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PreparedManifest:
    path: Path
    original_bytes: bytes
    original_sha256: str
    prepared_bytes: bytes
    prepared_sha256: str

    def verify_source(self) -> None:
        if not self.path.exists():
            raise MetadataTransactionError("Manifesto original não está disponível.")
        if hashlib.sha256(self.path.read_bytes()).hexdigest() != self.original_sha256:
            raise MetadataTransactionError("Manifesto foi alterado desde a preparação.")

    def verify_prepared(self) -> None:
        if hashlib.sha256(self.prepared_bytes).hexdigest() != self.prepared_sha256:
            raise MetadataTransactionError("Representação preparada do manifesto é inválida.")

    def verify_published(self, expected_sha256: str | None = None) -> str:
        actual = hashlib.sha256(self.path.read_bytes()).hexdigest()
        expected = expected_sha256 or self.prepared_sha256
        if actual != expected:
            raise MetadataTransactionError("Manifesto publicado não corresponde à versão esperada.")
        return actual

    def publish(self) -> str:
        self.verify_source()
        self.verify_prepared()
        temporary = Path(tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=self.path.parent)[1])
        try:
            with temporary.open("wb") as output:
                output.write(self.prepared_bytes)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise MetadataTransactionError("Não foi possível publicar o manifesto preparado.") from exc
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def restore(self, *, expected_current_sha256: str) -> str:
        current = hashlib.sha256(self.path.read_bytes()).hexdigest()
        if current != expected_current_sha256:
            raise MetadataTransactionError("Manifesto mudou antes da restauração.")
        temporary = Path(tempfile.mkstemp(prefix=".manifest-restore-", suffix=".tmp", dir=self.path.parent)[1])
        try:
            with temporary.open("wb") as output:
                output.write(self.original_bytes)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise MetadataTransactionError("Não foi possível restaurar o manifesto.") from exc
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


def prepare_manifest(path: str | Path, representation: dict[str, Any]) -> PreparedManifest:
    target = Path(path)
    try:
        original = target.read_bytes()
        prepared = (json.dumps(
            representation, ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n").encode("utf-8")
        json.loads(prepared.decode("utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise MetadataTransactionError("Manifesto não pôde ser preparado e validado.") from exc
    return PreparedManifest(
        path=target,
        original_bytes=original,
        original_sha256=hashlib.sha256(original).hexdigest(),
        prepared_bytes=prepared,
        prepared_sha256=hashlib.sha256(prepared).hexdigest(),
    )


def verify_manifest_operation(
    representation: dict[str, Any], operation_id: str,
    models: Iterable[SupplementModelIdentity],
) -> None:
    marker = representation.get("cfaz_digital_models")
    if not isinstance(marker, dict) or marker.get("operation_id") != operation_id:
        raise MetadataTransactionError("Manifesto não corresponde à operação esperada.")
    files = marker.get("provider_files")
    if not isinstance(files, dict):
        raise MetadataTransactionError("Manifesto não contém os modelos esperados.")
    for item in models:
        record = files.get(item.stl_file_id)
        if not isinstance(record, dict) or record.get("sha256") != item.sha256:
            raise MetadataTransactionError("Fingerprint do modelo não corresponde à operação.")


def _canonical_payload(
    *, provider: str, request_id: str,
    models: Iterable[SupplementModelIdentity],
) -> str:
    ordered = sorted(models, key=lambda item: item.semantic_role)
    if len(ordered) != 2 or len({item.semantic_role for item in ordered}) != 2:
        raise MetadataTransactionError("A operação exige dois papéis semânticos distintos.")
    values = {
        "algorithm": f"cfaz-supplement-operation-v{ALGORITHM_VERSION}",
        "provider": str(provider),
        "request_id": str(request_id),
        "models": [
            {
                "role": item.semantic_role,
                "stl_file_id": item.stl_file_id,
                "sha256": item.sha256,
                "destination": item.destination,
            }
            for item in ordered
        ],
    }
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def operation_identity(
    *, provider: str, request_id: str,
    models: Iterable[SupplementModelIdentity],
) -> tuple[str, str]:
    """Returns deterministic operation ID and its canonical, non-sensitive payload."""
    payload = _canonical_payload(provider=provider, request_id=request_id, models=models)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"cfaz-supplement-v{ALGORITHM_VERSION}-{digest}", payload


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SupplementOperationRepository:
    """Durable operation registry in the existing radiology SQLite database."""

    CAPABILITY = CAPABILITY

    def __init__(self, database_path: str) -> None:
        self.database_path = str(database_path)
        with self._connect() as connection:
            self.ensure_schema(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @staticmethod
    def ensure_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS cfaz_supplement_operations (
                operation_id TEXT PRIMARY KEY,
                algorithm_version INTEGER NOT NULL,
                provider TEXT NOT NULL,
                request_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                phase TEXT NOT NULL CHECK(phase IN (
                    'PREPARED','RADIOLOGY_COMMITTED','INTAKE_COMMITTED',
                    'MANIFEST_PUBLISHED','COMPLETED','COMPENSATING','REVIEW_REQUIRED')),
                phase_version INTEGER NOT NULL DEFAULT 0 CHECK(phase_version >= 0),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_cfaz_supplement_phase "
            "ON cfaz_supplement_operations(phase, updated_at)"
        )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> SupplementOperation | None:
        return SupplementOperation(**dict(row)) if row is not None else None

    def get(self, operation_id: str) -> SupplementOperation | None:
        with self._connect() as connection:
            return self._row(connection.execute(
                "SELECT * FROM cfaz_supplement_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone())

    def prepare(self, operation_id: str, payload_json: str, *, connection=None) -> SupplementOperation:
        def execute(db):
            existing = self._row(db.execute(
                "SELECT * FROM cfaz_supplement_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone())
            if existing is not None:
                if existing.payload_json != payload_json or existing.algorithm_version != ALGORITHM_VERSION:
                    raise MetadataTransactionError("A operação persistida possui payload incompatível.")
                return existing
            try:
                payload = json.loads(payload_json)
                provider = str(payload["provider"])
                request_id = str(payload["request_id"])
                identities = [
                    SupplementModelIdentity(
                        semantic_role=str(item["role"]),
                        stl_file_id=str(item["stl_file_id"]),
                        sha256=str(item["sha256"]),
                        destination=str(item["destination"]),
                    )
                    for item in payload["models"]
                ]
                expected_id, expected_payload = operation_identity(
                    provider=provider, request_id=request_id, models=identities
                )
                if expected_id != operation_id or expected_payload != payload_json:
                    raise MetadataTransactionError("Identidade da operação não corresponde ao payload.")
            except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                raise MetadataTransactionError("Payload de operação inválido.") from exc
            now = _now()
            db.execute(
                "INSERT INTO cfaz_supplement_operations VALUES(?,?,?,?,?,?,?,?,?)",
                (operation_id, ALGORITHM_VERSION, provider, request_id, payload_json,
                 "PREPARED", 0, now, now),
            )
            return self._row(db.execute(
                "SELECT * FROM cfaz_supplement_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone())
        if connection is not None:
            return execute(connection)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return execute(db)

    def transition(self, operation_id: str, expected_phase: str, new_phase: str, *, connection=None) -> SupplementOperation:
        if new_phase not in _TRANSITIONS.get(expected_phase, set()):
            raise MetadataTransactionError("Transição de fase inválida.")
        def execute(db):
            cursor = db.execute(
                "UPDATE cfaz_supplement_operations SET phase=?, phase_version=phase_version+1, updated_at=? "
                "WHERE operation_id=? AND phase=?",
                (new_phase, _now(), operation_id, expected_phase),
            )
            if cursor.rowcount != 1:
                raise MetadataTransactionError("A operação não está na fase esperada.")
            return self._row(db.execute(
                "SELECT * FROM cfaz_supplement_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone())
        if connection is not None:
            return execute(connection)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return execute(db)

    @staticmethod
    def allowed_transitions() -> dict[str, tuple[str, ...]]:
        return {phase: tuple(sorted(values)) for phase, values in _TRANSITIONS.items()}
