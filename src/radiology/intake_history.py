"""Histórico SQLite sanitizado e persistente do Radiology Intake."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
from typing import Iterable


SCHEMA_VERSION = 2
ALLOWED_STATUSES = {
    "DOWNLOADED", "EXTRACTED", "READY_FOR_CONFIRMATION", "COMPLETED",
    "FAILED", "CANCELLED", "DUPLICATE_DETECTED", "REIMPORT_CONFIRMED",
    "REVIEW_REQUIRED", "RESET",
}


class IntakeHistoryError(RuntimeError):
    """Falha operacional sanitizada no histórico local."""

    def __init__(self, message: str, *, operation: str = "HISTORY") -> None:
        super().__init__(message)
        self.operation = operation


def fingerprint(value: object | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def masked_filename(filename: str) -> str:
    path = Path(filename)
    return f"archive-{fingerprint(path.name)[:16]}{path.suffix.casefold()}"


@dataclass(frozen=True)
class IntakeRecord:
    id: int
    correlation_id: str
    run_id: str | None
    gmail_message_id_hash: str | None
    transfer_fingerprint: str | None
    archive_filename_masked: str
    archive_size: int
    archive_sha256: str | None
    patient_id_hash: str | None
    destination_fingerprint: str | None
    file_count: int | None
    total_size: int | None
    manifest_path_fingerprint: str | None
    file_checksums_fingerprint: str | None
    reimport_confirmed: bool
    previous_record_reference: str | None
    status: str
    reason_code: str | None
    stage: str | None
    exception_type: str | None
    created_at_utc: str
    updated_at_utc: str | None
    completed_at_utc: str | None
    operation_id: str | None = None
    operation_payload_hash: str | None = None

    @property
    def safe_reference(self) -> str:
        return f"intake-{fingerprint(self.id)[:12]}"


def _row_to_intake_record(row: sqlite3.Row) -> IntakeRecord:
    """Converte linhas dos schemas v1/v2 sem expor detalhes do banco."""
    values = dict(row)
    required_fields = {
        "id", "correlation_id", "archive_filename_masked", "archive_size",
        "status", "created_at_utc",
    }
    if required_fields.difference(values):
        raise IntakeHistoryError("Registro inválido no histórico local.", operation="ROW_MAP")

    if values["status"] not in ALLOWED_STATUSES:
        raise IntakeHistoryError("Status inválido no histórico local.", operation="ROW_MAP")

    optional_defaults = {
        "run_id": None,
        "gmail_message_id_hash": None,
        "transfer_fingerprint": None,
        "archive_sha256": None,
        "patient_id_hash": None,
        "destination_fingerprint": None,
        "file_count": None,
        "total_size": None,
        "manifest_path_fingerprint": None,
        "file_checksums_fingerprint": None,
        "reimport_confirmed": False,
        "previous_record_reference": None,
        "reason_code": None,
        "stage": None,
        "exception_type": None,
        "updated_at_utc": None,
        "completed_at_utc": None,
        "operation_id": None,
        "operation_payload_hash": None,
    }
    record_values = {
        field: values.get(field, default)
        for field, default in optional_defaults.items()
    }
    record_values.update({field: values[field] for field in required_fields})
    if record_values["reimport_confirmed"] is not None:
        record_values["reimport_confirmed"] = bool(record_values["reimport_confirmed"])
    return IntakeRecord(**record_values)


@dataclass(frozen=True)
class DuplicateMatch:
    criterion: str
    level: str
    record: IntakeRecord


class IntakeHistoryRepository:
    """Repositório SQLite sem dados clínicos em texto puro."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).expanduser()
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        except (OSError, sqlite3.Error) as exc:
            raise IntakeHistoryError(
                "O histórico local não pôde ser inicializado.", operation="SCHEMA_INITIALIZE"
            ) from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_info ("
                "key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
            )
            current = connection.execute(
                "SELECT value FROM schema_info WHERE key='schema_version'"
            ).fetchone()
            if current is not None and current["value"] > SCHEMA_VERSION:
                raise IntakeHistoryError("Versão do histórico local não suportada.")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS radiology_imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                correlation_id TEXT NOT NULL,
                run_id TEXT,
                gmail_message_id_hash TEXT,
                transfer_fingerprint TEXT,
                archive_filename_masked TEXT NOT NULL,
                archive_size INTEGER NOT NULL CHECK(archive_size >= 0),
                archive_sha256 TEXT,
                patient_id_hash TEXT,
                destination_fingerprint TEXT,
                file_count INTEGER CHECK(file_count IS NULL OR file_count >= 0),
                total_size INTEGER CHECK(total_size IS NULL OR total_size >= 0),
                manifest_path_fingerprint TEXT,
                file_checksums_fingerprint TEXT,
                reimport_confirmed INTEGER NOT NULL DEFAULT 0,
                previous_record_reference TEXT,
                status TEXT NOT NULL,
                reason_code TEXT,
                stage TEXT,
                exception_type TEXT,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT,
                completed_at_utc TEXT
                ,operation_id TEXT
                ,operation_payload_hash TEXT
                )"""
            )
            columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(radiology_imports)"
                )
            }
            migrations = {
                "run_id": "TEXT",
                "gmail_message_id_hash": "TEXT",
                "transfer_fingerprint": "TEXT",
                "archive_sha256": "TEXT",
                "patient_id_hash": "TEXT",
                "destination_fingerprint": "TEXT",
                "file_count": "INTEGER",
                "total_size": "INTEGER",
                "manifest_path_fingerprint": "TEXT",
                "file_checksums_fingerprint": "TEXT",
                "reimport_confirmed": "INTEGER NOT NULL DEFAULT 0",
                "previous_record_reference": "TEXT",
                "reason_code": "TEXT", "stage": "TEXT",
                "exception_type": "TEXT",
                "updated_at_utc": "TEXT",
                "completed_at_utc": "TEXT",
                "operation_id": "TEXT",
                "operation_payload_hash": "TEXT",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE radiology_imports ADD COLUMN {name} {definition}"
                    )
            required_columns = {
                "id", "correlation_id", "archive_filename_masked", "archive_size",
                "status", "created_at_utc",
            }
            if required_columns.difference(columns | migrations.keys()):
                raise IntakeHistoryError(
                    "Schema obrigatório incompleto no histórico local.",
                    operation="SCHEMA_VALIDATE",
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_intake_archive_sha "
                "ON radiology_imports(archive_sha256, status)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_intake_message "
                "ON radiology_imports(gmail_message_id_hash, status)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_intake_transfer "
                "ON radiology_imports(transfer_fingerprint, status)"
            )
            connection.execute(
                "INSERT INTO schema_info(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (SCHEMA_VERSION,),
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_intake_operation "
                "ON radiology_imports(operation_id) WHERE operation_id IS NOT NULL"
            )

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def create_downloaded(
        self, *, correlation_id: str, archive_filename: str, archive_size: int,
        archive_sha256: str | None, gmail_message_id: str | None = None,
        transfer_url: str | None = None, run_id: str | None = None,
    ) -> IntakeRecord:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """INSERT INTO radiology_imports (
                    correlation_id, gmail_message_id_hash, transfer_fingerprint,
                    archive_filename_masked, archive_size, archive_sha256,
                    run_id, status, created_at_utc
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'DOWNLOADED', ?)""",
                    (correlation_id, fingerprint(gmail_message_id),
                     fingerprint(transfer_url), masked_filename(archive_filename),
                     archive_size, archive_sha256, run_id, self._utc_now()),
                )
                record_id = cursor.lastrowid
            return self.get(int(record_id))
        except (OSError, sqlite3.Error) as exc:
            raise IntakeHistoryError(
                "Não foi possível registrar o intake.", operation="INSERT_DOWNLOADED"
            ) from exc

    def create_review(self, *, correlation_id: str, gmail_message_id: str | None,
                      reason_code: str, stage: str, run_id: str | None = None) -> IntakeRecord:
        """Registra pendência antes que exista arquivo local, sem dado clínico."""
        record = self.create_downloaded(
            correlation_id=correlation_id, archive_filename="unavailable.zip",
            archive_size=0, archive_sha256=None, gmail_message_id=gmail_message_id, run_id=run_id,
        )
        return self.update(record.id, "REVIEW_REQUIRED", reason_code=reason_code, stage=stage)

    def update(self, record_id: int, status: str, **fields: object) -> IntakeRecord:
        if status not in ALLOWED_STATUSES:
            raise IntakeHistoryError("Status de intake inválido.")
        allowed = {
            "patient_id_hash", "destination_fingerprint", "file_count", "total_size",
            "manifest_path_fingerprint", "archive_sha256",
            "file_checksums_fingerprint",
            "reimport_confirmed", "previous_record_reference",
            "reason_code", "stage", "exception_type", "run_id",
            "operation_id", "operation_payload_hash",
        }
        supplied = {key: value for key, value in fields.items() if key in allowed}
        supplied["status"] = status
        supplied["updated_at_utc"] = self._utc_now()
        if status == "COMPLETED":
            supplied["completed_at_utc"] = self._utc_now()
        assignments = ", ".join(f"{key}=?" for key in supplied)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    f"UPDATE radiology_imports SET {assignments} WHERE id=?",
                    (*supplied.values(), record_id),
                )
                if cursor.rowcount != 1:
                    raise IntakeHistoryError("Registro de intake não encontrado.")
            return self.get(record_id)
        except IntakeHistoryError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise IntakeHistoryError(
                "Não foi possível atualizar o histórico.", operation="UPDATE_STATUS"
            ) from exc

    def upsert_supplement(
        self, *, operation_id: str, payload_hash: str, correlation_id: str,
        archive_sha256: str | None, file_count: int, total_size: int,
        destination_fingerprint: str | None = None,
    ) -> IntakeRecord:
        """Creates or updates exactly one intake record for a supplement operation."""
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM radiology_imports WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if row is not None:
                    if row["operation_payload_hash"] != payload_hash:
                        raise IntakeHistoryError("Operação de suplemento conflitante no intake.")
                    record_id = int(row["id"])
                    connection.execute(
                        "UPDATE radiology_imports SET updated_at_utc=?, status='COMPLETED', "
                        "file_count=?, total_size=?, archive_sha256=?, destination_fingerprint=? WHERE id=?",
                        (self._utc_now(), file_count, total_size, archive_sha256,
                         destination_fingerprint, record_id),
                    )
                else:
                    try:
                        cursor = connection.execute(
                            """INSERT INTO radiology_imports(
                            correlation_id, archive_filename_masked, archive_size,
                            archive_sha256, file_count, total_size, destination_fingerprint,
                            status, created_at_utc, updated_at_utc, completed_at_utc,
                            operation_id, operation_payload_hash)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (correlation_id, "supplement-metadata", total_size,
                             archive_sha256, file_count, total_size,
                             destination_fingerprint, "COMPLETED", self._utc_now(),
                             self._utc_now(), self._utc_now(), operation_id, payload_hash),
                        )
                        record_id = int(cursor.lastrowid)
                    except sqlite3.IntegrityError as exc:
                        raise IntakeHistoryError("Operação de suplemento duplicada no intake.") from exc
            return self.get(record_id)
        except IntakeHistoryError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise IntakeHistoryError("Não foi possível registrar o suplemento no intake.") from exc

    def set_review_required(
        self, record_id: int, *, reason_code: str, stage: str,
        run_id: str | None = None,
    ) -> tuple[IntakeRecord, bool]:
        """Atualiza ou consolida uma única pendência ativa equivalente."""
        now = self._utc_now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT * FROM radiology_imports WHERE id=?", (record_id,)
                ).fetchone()
                if current is None:
                    raise IntakeHistoryError("Registro de intake não encontrado.")
                current_record = _row_to_intake_record(current)
                if current_record.status in {"COMPLETED", "REIMPORT_CONFIRMED"}:
                    return current_record, False

                existing = None
                if (current_record.archive_sha256
                        and current_record.gmail_message_id_hash):
                    existing = connection.execute(
                        """SELECT * FROM radiology_imports
                        WHERE id<>? AND status='REVIEW_REQUIRED'
                        AND archive_sha256=? AND gmail_message_id_hash=?
                        AND patient_id_hash IS ? AND reason_code=? AND stage=?
                        ORDER BY id ASC LIMIT 1""",
                        (record_id, current_record.archive_sha256,
                         current_record.gmail_message_id_hash,
                         current_record.patient_id_hash, reason_code, stage),
                    ).fetchone()
                if existing is not None:
                    existing_id = int(existing["id"])
                    connection.execute(
                        "UPDATE radiology_imports SET updated_at_utc=?, run_id=? WHERE id=?",
                        (now, run_id, existing_id),
                    )
                    connection.execute(
                        "DELETE FROM radiology_imports WHERE id=?", (record_id,)
                    )
                    selected_id, skipped = existing_id, True
                else:
                    connection.execute(
                        """UPDATE radiology_imports
                        SET status='REVIEW_REQUIRED', reason_code=?, stage=?,
                            run_id=?, updated_at_utc=? WHERE id=?""",
                        (reason_code, stage, run_id, now, record_id),
                    )
                    selected_id, skipped = record_id, False
            return self.get(selected_id), skipped
        except IntakeHistoryError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise IntakeHistoryError(
                "Não foi possível consolidar a pendência.",
                operation="UPSERT_REVIEW_REQUIRED",
            ) from exc

    def get(self, record_id: int) -> IntakeRecord:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM radiology_imports WHERE id=?", (record_id,)
                ).fetchone()
            if row is None:
                raise IntakeHistoryError("Registro de intake não encontrado.")
            return _row_to_intake_record(row)
        except IntakeHistoryError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise IntakeHistoryError(
                "Não foi possível consultar o histórico.", operation="SELECT_BY_ID"
            ) from exc

    def find_latest_by_message(self, gmail_message_id: str) -> IntakeRecord | None:
        """Consulta a identificação mínima antes de iniciar um novo download."""
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM radiology_imports WHERE gmail_message_id_hash=? "
                    "ORDER BY id DESC LIMIT 1",
                    (fingerprint(gmail_message_id),),
                ).fetchone()
            return _row_to_intake_record(row) if row is not None else None
        except IntakeHistoryError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise IntakeHistoryError(
                "Não foi possível consultar o histórico.", operation="SELECT_MESSAGE_STATE"
            ) from exc

    def find_duplicate(
        self, *, archive_sha256: str | None = None,
        gmail_message_id: str | None = None, archive_filename: str | None = None,
        transfer_url: str | None = None,
        archive_size: int | None = None, patient_id: object | None = None,
        destination: object | None = None,
    ) -> DuplicateMatch | None:
        checks = (
            ("ARCHIVE_SHA256", "confirmed", "archive_sha256", archive_sha256),
            ("GMAIL_MESSAGE_ID", "confirmed", "gmail_message_id_hash", fingerprint(gmail_message_id)),
            ("TRANSFER_FINGERPRINT", "confirmed", "transfer_fingerprint", fingerprint(transfer_url)),
            ("DESTINATION", "possible", "destination_fingerprint", fingerprint(destination)),
        )
        try:
            with self._connect() as connection:
                for criterion, level, column, value in checks:
                    if not value:
                        continue
                    row = connection.execute(
                        f"SELECT * FROM radiology_imports WHERE {column}=? "
                        "AND status='COMPLETED' ORDER BY id DESC LIMIT 1", (value,)
                    ).fetchone()
                    if row:
                        return DuplicateMatch(criterion, level, _row_to_intake_record(row))
                if archive_filename and archive_size is not None and patient_id is not None:
                    row = connection.execute(
                        """SELECT * FROM radiology_imports
                        WHERE archive_filename_masked=? AND archive_size=?
                        AND patient_id_hash=? AND status='COMPLETED'
                        ORDER BY id DESC LIMIT 1""",
                        (masked_filename(archive_filename), archive_size, fingerprint(patient_id)),
                    ).fetchone()
                    if row:
                        return DuplicateMatch(
                            "ARCHIVE_NAME_SIZE_PATIENT", "possible", _row_to_intake_record(row)
                        )
            return None
        except (OSError, sqlite3.Error) as exc:
            raise IntakeHistoryError(
                "Não foi possível verificar duplicidades.", operation="SELECT_DUPLICATE"
            ) from exc

    def list_records(
        self, *, limit: int = 20, status: str | None = None,
        correlation_id: str | None = None,
    ) -> list[IntakeRecord]:
        clauses, values = [], []
        if status:
            if status not in ALLOWED_STATUSES:
                raise IntakeHistoryError("Status de intake inválido.")
            clauses.append("status=?")
            values.append(status)
        if correlation_id:
            candidate = correlation_id.removeprefix("correlation-")
            if correlation_id.startswith("correlation-"):
                clauses.append("(correlation_id=? OR correlation_id LIKE ?)")
                values.extend((correlation_id, f"{candidate}%"))
            else:
                clauses.append("correlation_id=?")
                values.append(correlation_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    f"SELECT * FROM radiology_imports{where} ORDER BY id DESC LIMIT ?",
                    (*values, min(max(int(limit), 1), 100)),
                ).fetchall()
            return [_row_to_intake_record(row) for row in rows]
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise IntakeHistoryError(
                "Não foi possível consultar o histórico.", operation="SELECT_HISTORY"
            ) from exc


def sanitized_history_lines(records: Iterable[IntakeRecord]) -> list[str]:
    return [
        " | ".join((
            record.safe_reference,
            f"correlation-{record.correlation_id[:12]}",
            record.created_at_utc,
            record.status,
            record.reason_code or "reason-unavailable",
            record.stage or "stage-unavailable",
            record.archive_filename_masked,
            f"patient-{record.patient_id_hash[:12]}" if record.patient_id_hash else "patient-unavailable",
            f"destination-{record.destination_fingerprint[:12]}" if record.destination_fingerprint else "destination-unavailable",
        ))
        for record in records
    ]
