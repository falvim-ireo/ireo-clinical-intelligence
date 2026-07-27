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
    "COMPLETED": {"REVIEW_REQUIRED"},
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
    preparation_version: int | None = None
    manifest_original_sha256: str | None = None
    manifest_prepared_sha256: str | None = None
    prepared_delta_json: str | None = None
    payload_hash: str | None = None


PREPARATION_VERSION = 1


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


def validate_prepared_representation(
    representation: dict[str, Any], operation_id: str,
    models: Iterable[SupplementModelIdentity],
) -> None:
    """Validate the technical supplement marker before any local mutation."""
    verify_manifest_operation(representation, operation_id, models)
    marker = representation["cfaz_digital_models"]
    for key in marker:
        if key not in {"operation_id", "provider_files"}:
            raise MetadataTransactionError("Representação preparada contém metadado não permitido.")
    for file_id, value in marker["provider_files"].items():
        if not isinstance(value, dict) or set(value) - {"sha256", "stl_file_id", "destination"}:
            raise MetadataTransactionError("Representação preparada contém metadado não permitido.")
        if value.get("stl_file_id", file_id) != file_id or "destination" not in value:
            raise MetadataTransactionError("Representação preparada não contém identidade técnica completa.")
        for field in ("stl_file_id", "destination", "sha256"):
            if not isinstance(value.get(field), str) or any(token in value[field].casefold() for token in ("token", "cookie", "etag", "://")):
                raise MetadataTransactionError("Representação preparada contém dado operacional não permitido.")
        destination = value["destination"]
        if destination.startswith(("/", "~")) or ".." in destination.replace("\\", "/").split("/"):
            raise MetadataTransactionError("Destino lógico absoluto ou temporário não é permitido.")


def _prepared_delta(original: dict[str, Any], prepared: dict[str, Any]) -> dict[str, Any]:
    """Return the only permitted deterministic change: the technical marker."""
    base = dict(original)
    marker = prepared.get("cfaz_digital_models")
    if not isinstance(marker, dict):
        raise MetadataTransactionError("Manifesto preparado sem marcador técnico.")
    if any(key != "cfaz_digital_models" and original.get(key) != prepared.get(key) for key in set(original) | set(prepared)):
        raise MetadataTransactionError("Preparação altera conteúdo fora da allowlist técnica.")
    return {"cfaz_digital_models": marker}


def verify_manifest_operation(
    representation: dict[str, Any], operation_id: str,
    models: Iterable[SupplementModelIdentity],
) -> None:
    marker = representation.get("cfaz_digital_models")
    if not isinstance(marker, dict) or marker.get("operation_id") != operation_id:
        raise MetadataTransactionError("Manifesto não corresponde à operação esperada.")
    models = tuple(models)
    files = marker.get("provider_files")
    if not isinstance(files, dict):
        raise MetadataTransactionError("Manifesto não contém os modelos esperados.")
    expected_ids = {item.stl_file_id for item in models}
    if set(files) != expected_ids:
        raise MetadataTransactionError("Manifesto não contém exatamente os modelos esperados.")
    for item in models:
        record = files.get(item.stl_file_id)
        if (not isinstance(record, dict)
                or record.get("sha256") != item.sha256
                or ("stl_file_id" in record and record.get("stl_file_id") != item.stl_file_id)
                or ("destination" in record and record.get("destination") != item.destination)):
            raise MetadataTransactionError("Fingerprint do modelo não corresponde à operação.")


def _canonical_payload(
    *, provider: str, request_id: str,
    models: Iterable[SupplementModelIdentity],
) -> str:
    ordered = sorted(models, key=lambda item: item.semantic_role)
    if (len(ordered) != 2
            or len({item.semantic_role for item in ordered}) != 2
            or len({item.stl_file_id for item in ordered}) != 2
            or len({item.destination for item in ordered}) != 2):
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
        columns = {row[1] for row in connection.execute(
            "PRAGMA table_info(cfaz_supplement_operations)"
        )}
        additions = {
            "preparation_version": "INTEGER",
            "manifest_original_sha256": "TEXT",
            "manifest_prepared_sha256": "TEXT",
            "prepared_delta_json": "TEXT",
            "payload_hash": "TEXT",
        }
        for name, kind in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE cfaz_supplement_operations ADD COLUMN {name} {kind}"
                )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS cfaz_supplement_destination_claims(
                destination_key TEXT PRIMARY KEY,
                operation_id TEXT NOT NULL,
                FOREIGN KEY(operation_id) REFERENCES cfaz_supplement_operations(operation_id)
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_cfaz_destination_claim_operation "
            "ON cfaz_supplement_destination_claims(operation_id)"
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
                "INSERT INTO cfaz_supplement_operations(operation_id,algorithm_version,provider,request_id,payload_json,phase,phase_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
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

    def transition(self, operation_id: str, expected_phase: str, new_phase: str, *, expected_version: int | None = None, connection=None) -> SupplementOperation:
        if new_phase not in _TRANSITIONS.get(expected_phase, set()):
            raise MetadataTransactionError("Transição de fase inválida.")
        def execute(db):
            clause = "WHERE operation_id=? AND phase=?"
            values: list[Any] = [new_phase, _now(), operation_id, expected_phase]
            if expected_version is not None:
                clause += " AND phase_version=?"
                values.append(expected_version)
            cursor = db.execute(
                "UPDATE cfaz_supplement_operations SET phase=?, phase_version=phase_version+1, updated_at=? "
                + clause, values,
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

    def persist_preparation(
        self, operation_id: str, *, original_sha256: str, prepared_sha256: str,
        prepared_delta: dict[str, Any], payload_hash: str,
        connection=None,
    ) -> SupplementOperation:
        delta_json = json.dumps(prepared_delta, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        def execute(db):
            row = self._row(db.execute(
                "SELECT * FROM cfaz_supplement_operations WHERE operation_id=?", (operation_id,)
            ).fetchone())
            if row is None:
                raise MetadataTransactionError("Operação persistida não foi encontrada.")
            values = (PREPARATION_VERSION, original_sha256, prepared_sha256, delta_json, payload_hash)
            existing = (row.preparation_version, row.manifest_original_sha256,
                        row.manifest_prepared_sha256, row.prepared_delta_json, row.payload_hash)
            if any(item is not None for item in existing) and existing != values:
                raise MetadataTransactionError("Âncora de preparação incompatível.")
            db.execute(
                "UPDATE cfaz_supplement_operations SET preparation_version=?, "
                "manifest_original_sha256=?, manifest_prepared_sha256=?, prepared_delta_json=?, payload_hash=? "
                "WHERE operation_id=?",
                (*values, operation_id),
            )
            return self._row(db.execute(
                "SELECT * FROM cfaz_supplement_operations WHERE operation_id=?", (operation_id,)
            ).fetchone())
        if connection is not None:
            return execute(connection)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return execute(db)

    def reserve_destinations(self, operation_id: str, destinations: Iterable[str], *, connection=None) -> None:
        keys = tuple(sorted({hashlib.sha256(str(value).encode("utf-8")).hexdigest() for value in destinations}))
        if len(keys) != 2:
            raise MetadataTransactionError("A operação exige dois destinos lógicos distintos.")
        def execute(db):
            for key in keys:
                row = db.execute(
                    "SELECT operation_id FROM cfaz_supplement_destination_claims WHERE destination_key=?",
                    (key,),
                ).fetchone()
                if row is not None and row["operation_id"] != operation_id:
                    raise MetadataTransactionError("Destino lógico já reivindicado por outra operação.")
                db.execute(
                    "INSERT OR IGNORE INTO cfaz_supplement_destination_claims(destination_key,operation_id) VALUES(?,?)",
                    (key, operation_id),
                )
        if connection is not None:
            execute(connection)
        else:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                execute(db)

    def destination_claims(self, operation_id: str) -> tuple[str, ...]:
        with self._connect() as db:
            return tuple(row[0] for row in db.execute(
                "SELECT destination_key FROM cfaz_supplement_destination_claims WHERE operation_id=? ORDER BY destination_key",
                (operation_id,),
            ))

    def register_prepared(
        self, operation_id: str, payload_json: str, *, original_sha256: str,
        prepared_sha256: str, prepared_delta: dict[str, Any], payload_hash: str,
        destinations: Iterable[str],
    ) -> SupplementOperation:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            operation = self.prepare(operation_id, payload_json, connection=db)
            self.persist_preparation(
                operation_id, original_sha256=original_sha256,
                prepared_sha256=prepared_sha256, prepared_delta=prepared_delta,
                payload_hash=payload_hash, connection=db,
            )
            self.reserve_destinations(operation_id, destinations, connection=db)
            return self._row(db.execute(
                "SELECT * FROM cfaz_supplement_operations WHERE operation_id=?", (operation_id,)
            ).fetchone())


@dataclass(frozen=True)
class LocalMetadataCoordinatorResult:
    operation_id: str
    phase: str
    idempotent: bool = False
    status: str = "COMPLETED"


class LocalMetadataCoordinator:
    """Reentrant local metadata saga; deliberately not a productive adapter.

    The coordinator composes only the already-persistent local stores.  It has
    no network capability and is intentionally not registered with the CLI
    productive-coordinator resolver.
    """

    CAPABILITY = "cfaz-supplement-local-saga-v1"

    def __init__(
        self, *, operations: SupplementOperationRepository, index: Any,
        history: Any, intake: Any, manifest_path: str | Path,
        provider: str, request_id: str,
        models: Iterable[SupplementModelIdentity],
        prepared_representation: dict[str, Any],
        checkpoint: Any | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.operations = operations
        self.index = index
        self.history = history
        self.intake = intake
        self.manifest_path = Path(manifest_path)
        self.provider = str(provider)
        self.request_id = str(request_id)
        self.models = tuple(models)
        self.prepared_representation = prepared_representation
        self.checkpoint = checkpoint
        self.correlation_id = correlation_id or f"cfaz-supplement-{self.request_id}"
        self.operation_id, self.payload_json = operation_identity(
            provider=self.provider, request_id=self.request_id, models=self.models
        )
        self.payload_hash = hashlib.sha256(self.payload_json.encode("utf-8")).hexdigest()

    def _event(self, name: str) -> None:
        if self.checkpoint is not None:
            self.checkpoint(name)

    def _advance(self, operation: SupplementOperation, new_phase: str) -> None:
        try:
            self.operations.transition(
                self.operation_id, operation.phase, new_phase,
                expected_version=operation.phase_version,
            )
        except MetadataTransactionError:
            latest = self.operations.get(self.operation_id)
            if latest is not None and latest.phase != operation.phase:
                return
            raise

    def _review(self, message: str) -> None:
        operation = self.operations.get(self.operation_id)
        if operation is not None and operation.phase != "REVIEW_REQUIRED":
            self.operations.transition(
                self.operation_id, operation.phase, "REVIEW_REQUIRED",
                expected_version=operation.phase_version,
            )
        raise MetadataTransactionError(message)

    def _prepared_from_anchor(self, operation: SupplementOperation) -> PreparedManifest:
        if (operation.preparation_version != PREPARATION_VERSION
                or not operation.manifest_original_sha256
                or not operation.manifest_prepared_sha256
                or not operation.prepared_delta_json):
            raise MetadataTransactionError("Operação PREPARED não possui âncora de manifesto recuperável.")
        if operation.payload_hash != self.payload_hash:
            raise MetadataTransactionError("Âncora de manifesto não corresponde ao payload da operação.")
        try:
            current = self.manifest_path.read_bytes()
            current_hash = hashlib.sha256(current).hexdigest()
            if current_hash == operation.manifest_prepared_sha256:
                parsed = json.loads(current.decode("utf-8"))
                validate_prepared_representation(parsed, self.operation_id, self.models)
                expected_delta = json.dumps(
                    {"cfaz_digital_models": parsed["cfaz_digital_models"]},
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                )
                if expected_delta != operation.prepared_delta_json:
                    raise MetadataTransactionError("Âncora de manifesto não corresponde ao conteúdo publicado.")
                return PreparedManifest(self.manifest_path, current, operation.manifest_original_sha256,
                                        current, operation.manifest_prepared_sha256)
            if current_hash != operation.manifest_original_sha256:
                raise MetadataTransactionError("Manifesto alterado desde a preparação persistida.")
            original = json.loads(current.decode("utf-8"))
            delta = json.loads(operation.prepared_delta_json)
            if set(delta) != {"cfaz_digital_models"} or not isinstance(delta["cfaz_digital_models"], dict):
                raise MetadataTransactionError("Âncora de manifesto inválida.")
            representation = dict(original)
            representation["cfaz_digital_models"] = delta["cfaz_digital_models"]
            validate_prepared_representation(representation, self.operation_id, self.models)
            prepared = prepare_manifest(self.manifest_path, representation)
            if (prepared.original_sha256 != operation.manifest_original_sha256
                    or prepared.prepared_sha256 != operation.manifest_prepared_sha256):
                raise MetadataTransactionError("Manifesto não pôde ser reconstruído pela âncora persistida.")
            return prepared
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise MetadataTransactionError("Âncora de manifesto não pôde ser carregada.") from exc

    def _initial_prepared(self) -> tuple[PreparedManifest, dict[str, Any]]:
        prepared = prepare_manifest(self.manifest_path, self.prepared_representation)
        try:
            original = json.loads(prepared.original_bytes.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise MetadataTransactionError("Manifesto original não é JSON válido.") from exc
        validate_prepared_representation(self.prepared_representation, self.operation_id, self.models)
        return prepared, _prepared_delta(original, self.prepared_representation)

    def _radiology(self, operation: SupplementOperation, prepared: PreparedManifest) -> None:
        index_db = Path(self.index.database_path).resolve()
        history_db = Path(self.history.database_path).resolve()
        if index_db != history_db or index_db != Path(self.operations.database_path).resolve():
            raise MetadataTransactionError("Índice, histórico CFAZ e operação não compartilham o SQLite radiológico.")
        if len(self.operations.destination_claims(self.operation_id)) != 2:
            raise MetadataTransactionError("Claims persistentes dos destinos estão incompletos.")
        prepared.verify_source()
        prepared.verify_prepared()
        db = sqlite3.connect(index_db, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT phase,phase_version FROM cfaz_supplement_operations WHERE operation_id=?",
                (self.operation_id,),
            ).fetchone()
            if (current is None or current["phase"] != operation.phase
                    or current["phase_version"] != operation.phase_version):
                db.rollback()
                return
            self.index.index_manifest(
                self.prepared_representation, source="cfaz-supplement",
                connection=db, operation_id=self.operation_id,
                operation_payload_hash=self.payload_hash, request_id=self.request_id,
                cfaz_models=({
                    "semantic_role": item.semantic_role,
                    "stl_file_id": item.stl_file_id,
                    "sha256": item.sha256,
                    "destination": item.destination,
                } for item in self.models),
            )
            self.history.record_supplement(
                request_id=self.request_id, operation_id=self.operation_id,
                payload_hash=self.payload_hash, connection=db,
            )
            self.operations.transition(
                self.operation_id, operation.phase, "RADIOLOGY_COMMITTED",
                expected_version=operation.phase_version, connection=db,
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        self._event("RADIOLOGY_COMMITTED")

    def _intake(self, operation: SupplementOperation) -> None:
        from radiology.intake_history import IntakeHistoryError
        existing = self.intake.get_by_operation(self.operation_id)
        if existing is not None:
            if not self.intake.verify_supplement(
                operation_id=self.operation_id, payload_hash=self.payload_hash,
                correlation_id=self.correlation_id, file_count=2, total_size=0,
            ):
                self._review("Estado conflitante no intake exige revisão manual.")
            self._event("INTAKE_COMMITTED")
            self._advance(operation, "INTAKE_COMMITTED")
            return
        try:
            self.intake.upsert_supplement(
                operation_id=self.operation_id, payload_hash=self.payload_hash,
                correlation_id=self.correlation_id, archive_sha256=None,
                file_count=2, total_size=0,
            )
        except IntakeHistoryError as exc:
            if "conflit" in str(exc).casefold() or "duplicad" in str(exc).casefold():
                self._review("Estado conflitante no intake exige revisão manual.")
            raise
        self._event("INTAKE_COMMITTED")
        self._advance(operation, "INTAKE_COMMITTED")

    def _manifest(self, operation: SupplementOperation, prepared: PreparedManifest) -> None:
        try:
            current = prepared.path.read_bytes()
            current_hash = hashlib.sha256(current).hexdigest()
            if current_hash == prepared.prepared_sha256:
                representation = json.loads(current.decode("utf-8"))
                validate_prepared_representation(representation, self.operation_id, self.models)
            elif current_hash == prepared.original_sha256:
                try:
                    prepared.publish()
                except MetadataTransactionError:
                    # Another worker may have published the same canonical
                    # representation between our optimistic read and replace.
                    reconciled = hashlib.sha256(prepared.path.read_bytes()).hexdigest()
                    if reconciled != prepared.prepared_sha256:
                        self._review("Manifesto alterado durante publicação; revisão manual necessária.")
                    validate_prepared_representation(
                        json.loads(prepared.path.read_text(encoding="utf-8")),
                        self.operation_id, self.models,
                    )
            else:
                self._review("Manifesto alterado por outra operação; publicação bloqueada.")
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise MetadataTransactionError("Manifesto publicado não pôde ser reconciliado.") from exc
        self._event("MANIFEST_PUBLISHED")
        self._advance(operation, "MANIFEST_PUBLISHED")

    def _verify_all(self, prepared: PreparedManifest) -> None:
        representation = None
        try:
            prepared.verify_published()
            representation = json.loads(prepared.path.read_text(encoding="utf-8"))
            validate_prepared_representation(representation, self.operation_id, self.models)
        except (OSError, UnicodeError, json.JSONDecodeError, MetadataTransactionError) as exc:
            self._review("Verificação final do manifesto falhou; revisão manual necessária.")
        if representation is None or not self.index.verify_operation_manifest(
            self.operation_id, representation, request_id=self.request_id,
            payload_hash=self.payload_hash, models=self.models,
        ):
            self._review("Verificação final do índice falhou; revisão manual necessária.")
        if not self.history.verify_supplement(
            request_id=self.request_id, operation_id=self.operation_id,
            payload_hash=self.payload_hash,
        ):
            self._review("Verificação final do histórico CFAZ falhou; revisão manual necessária.")
        if not self.intake.verify_supplement(
            operation_id=self.operation_id, payload_hash=self.payload_hash,
            correlation_id=self.correlation_id, file_count=2, total_size=0,
        ):
            self._review("Verificação final do intake falhou; revisão manual necessária.")

    def _run(self, *, resumed: bool) -> LocalMetadataCoordinatorResult:
        existing = self.operations.get(self.operation_id)
        if resumed and existing is None:
            raise MetadataTransactionError("Não existe operação persistida para resume().")
        if existing is None:
            prepared, delta = self._initial_prepared()
            operation = self.operations.register_prepared(
                self.operation_id, self.payload_json,
                original_sha256=prepared.original_sha256,
                prepared_sha256=prepared.prepared_sha256,
                prepared_delta=delta, payload_hash=self.payload_hash,
                destinations=(item.destination for item in self.models),
            )
        else:
            operation = self.operations.prepare(self.operation_id, self.payload_json)
            try:
                prepared = self._prepared_from_anchor(operation)
            except MetadataTransactionError as exc:
                if operation.phase != "REVIEW_REQUIRED":
                    self._review("Preparação persistida não pôde ser reconciliada; revisão manual necessária.")
                raise exc
            expected_claims = tuple(sorted(
                hashlib.sha256(item.destination.encode("utf-8")).hexdigest()
                for item in self.models
            ))
            if self.operations.destination_claims(self.operation_id) != expected_claims:
                self._review("Claims de destino ausentes ou conflitantes exigem revisão manual.")
        if operation.phase == "COMPLETED":
            self._verify_all(prepared)
            return LocalMetadataCoordinatorResult(self.operation_id, operation.phase, True, "ALREADY_COMPLETE")
        if operation.phase == "REVIEW_REQUIRED":
            raise MetadataTransactionError("Operação requer revisão manual.")
        while operation.phase != "COMPLETED":
            if operation.phase == "PREPARED":
                self._radiology(operation, prepared)
            elif operation.phase == "RADIOLOGY_COMMITTED":
                self._intake(operation)
            elif operation.phase == "INTAKE_COMMITTED":
                self._manifest(operation, prepared)
            elif operation.phase == "MANIFEST_PUBLISHED":
                self._verify_all(prepared)
                self._advance(operation, "COMPLETED")
            else:
                raise MetadataTransactionError("Fase persistida não é recuperável automaticamente.")
            operation = self.operations.get(self.operation_id)
            if operation is None:
                raise MetadataTransactionError("Operação persistida desapareceu durante a execução.")
        return LocalMetadataCoordinatorResult(self.operation_id, operation.phase, False)

    def execute(self) -> LocalMetadataCoordinatorResult:
        """Start or continue the saga from the durable phase."""
        return self._run(resumed=False)

    def resume(self) -> LocalMetadataCoordinatorResult:
        """Resume with caller-recreated local repositories and manifest reference."""
        return self._run(resumed=True)
