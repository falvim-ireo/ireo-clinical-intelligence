"""Catálogo e histórico operacional do Cfaz, sem expor IDs internos ao operador."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import getaddresses
import hashlib
from pathlib import Path
import re
import sqlite3
from typing import Iterable

from acquisition.cfaz_provider import CfazProvider, CfazRequestError
from models.email_message import EmailMessage


class CfazHistoryError(RuntimeError): pass


@dataclass(frozen=True)
class CfazNotification:
    message: EmailMessage
    request_id: str | None
    patient_name: str | None
    received_at: datetime
    imported: bool
    accepted: bool = True
    rejection_reason: str | None = None
    subject_recognized: bool = True
    link_found: bool = True


@dataclass(frozen=True)
class CfazNotificationCounters:
    gmail_messages: int
    cfazpost_subjects: int
    recognized_links: int
    operational_notifications: int


@dataclass(frozen=True)
class CfazHistoryRecord:
    request_id: str
    provider: str
    patient_name: str | None
    status: str
    started_at: str | None
    completed_at: str | None
    duration_seconds: float | None
    onedrive_destination: str | None
    provider_exam_id: str | None = None
    acquisition_sha: str | None = None
    import_timestamp: str | None = None
    provider_request_id: str | None = None
    sequential_id: str | None = None
    clinic_number: str | None = None
    repaired_at: str | None = None
    repair_version: int | None = None
    supplement_operation_id: str | None = None
    supplement_payload_hash: str | None = None


class CfazHistoryRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as db:
                db.execute(
                    """CREATE TABLE IF NOT EXISTS cfaz_import_history (
                    provider TEXT NOT NULL, request_id TEXT NOT NULL,
                    provider_request_id TEXT, sequential_id TEXT, clinic_number TEXT,
                    repaired_at TEXT, repair_version INTEGER,
                    provider_exam_id TEXT, acquisition_sha TEXT,
                    import_timestamp TEXT,
                    message_id_hash TEXT, patient_name TEXT,
                    status TEXT NOT NULL, started_at TEXT, completed_at TEXT,
                    duration_seconds REAL, onedrive_destination TEXT,
                    error_code TEXT, updated_at TEXT NOT NULL,
                    supplement_operation_id TEXT,
                    supplement_payload_hash TEXT,
                    PRIMARY KEY(provider, request_id))"""
                )
                columns = {
                    row[1] for row in db.execute("PRAGMA table_info(cfaz_import_history)")
                }
                migrations = {
                    "provider_request_id": "TEXT", "sequential_id": "TEXT",
                    "clinic_number": "TEXT", "repaired_at": "TEXT",
                    "repair_version": "INTEGER",
                    "supplement_operation_id": "TEXT",
                    "supplement_payload_hash": "TEXT",
                }
                for column, sql_type in migrations.items():
                    if column not in columns:
                        db.execute(
                            f"ALTER TABLE cfaz_import_history ADD COLUMN {column} {sql_type}"
                        )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cfaz_history_status "
                    "ON cfaz_import_history(provider,status,updated_at)"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cfaz_history_provider_request "
                    "ON cfaz_import_history(provider,provider_request_id,status)"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cfaz_history_sequential "
                    "ON cfaz_import_history(provider,sequential_id,status)"
                )
                db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_cfaz_supplement_operation "
                    "ON cfaz_import_history(supplement_operation_id) "
                    "WHERE supplement_operation_id IS NOT NULL"
                )
        except (OSError, sqlite3.Error) as exc:
            raise CfazHistoryError("Não foi possível abrir o histórico Cfaz.") from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def is_imported(self, request_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                """SELECT status FROM cfaz_import_history
                WHERE provider='cfaz' AND
                (request_id=? OR provider_request_id=? OR sequential_id=?)
                ORDER BY status='COMPLETE' DESC LIMIT 1""",
                (request_id, request_id, request_id),
            ).fetchone()
        return bool(row and row["status"] == "COMPLETE")

    def mark_started(
        self, *, request_id: str, message_id: str | None,
        patient_name: str | None, provider_exam_id: str | None = None,
        provider_request_id: str | None = None,
        sequential_id: str | None = None, clinic_number: str | None = None,
    ) -> None:
        now = self._now()
        message_hash = (
            hashlib.sha256(message_id.encode()).hexdigest() if message_id else None
        )
        self._upsert(
            request_id=request_id, message_id_hash=message_hash,
            patient_name=patient_name, provider_exam_id=provider_exam_id,
            provider_request_id=provider_request_id or request_id,
            sequential_id=sequential_id, clinic_number=clinic_number,
            acquisition_sha=None, import_timestamp=None, status="IN_PROGRESS",
            started_at=now, completed_at=None, duration_seconds=None,
            onedrive_destination=None, error_code=None,
        )

    def mark_complete(
        self, *, request_id: str, patient_name: str | None,
        duration_seconds: float, onedrive_destination: str,
        provider_exam_id: str | None = None,
        acquisition_sha: str | None = None,
        provider_request_id: str | None = None,
        sequential_id: str | None = None, clinic_number: str | None = None,
    ) -> None:
        self._update_terminal(
            request_id, patient_name, "COMPLETE", duration_seconds,
            onedrive_destination, None, provider_exam_id, acquisition_sha,
            provider_request_id or request_id, sequential_id, clinic_number,
        )

    def mark_failed(
        self, *, request_id: str, patient_name: str | None,
        duration_seconds: float, error_code: str,
    ) -> None:
        self._update_terminal(
            request_id, patient_name, "FAILED", duration_seconds, None,
            error_code[:128], None, None, request_id, None, None,
        )

    def mark_ambiguous(self, sequential_id: str) -> None:
        self._upsert(
            request_id=f"sequential:{sequential_id}", patient_name=None,
            provider_request_id=None, sequential_id=sequential_id,
            clinic_number=None, status="AMBIGUOUS", started_at=self._now(),
            completed_at=None, duration_seconds=None, onedrive_destination=None,
            error_code="AMBIGUOUS", acquisition_sha=None, import_timestamp=None,
        )

    def mark_ready_for_reimport(self, identifier: str) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE cfaz_import_history SET status='READY_FOR_REIMPORT',
                updated_at=?, error_code=NULL WHERE provider='cfaz' AND
                (request_id=? OR provider_request_id=? OR sequential_id=?)""",
                (self._now(), str(identifier), str(identifier), str(identifier)),
            )

    def mark_repaired(self, identifier: str, repair_version: int) -> None:
        now = self._now()
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE cfaz_import_history SET repaired_at=?,repair_version=?,
                updated_at=? WHERE provider='cfaz' AND status='COMPLETE' AND
                (request_id=? OR provider_request_id=? OR sequential_id=?)""",
                (now, repair_version, now, identifier, identifier, identifier),
            )
            if cursor.rowcount != 1:
                raise CfazHistoryError(
                    "O histórico COMPLETE do pedido Cfaz não foi localizado de forma única."
                )

    def record_supplement(
        self, *, request_id: str, operation_id: str, payload_hash: str,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Associates one idempotent supplement with the existing CFAZ row."""
        def execute(db: sqlite3.Connection) -> None:
            row = db.execute(
                "SELECT supplement_operation_id,supplement_payload_hash FROM cfaz_import_history "
                "WHERE provider='cfaz' AND request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise CfazHistoryError("Histórico CFAZ não encontrado para o suplemento.")
            if row["supplement_operation_id"] is not None:
                if row["supplement_operation_id"] != operation_id or row["supplement_payload_hash"] != payload_hash:
                    raise CfazHistoryError("Operação de suplemento conflitante no histórico CFAZ.")
                return
            try:
                db.execute(
                    "UPDATE cfaz_import_history SET supplement_operation_id=?, supplement_payload_hash=?, updated_at=? "
                    "WHERE provider='cfaz' AND request_id=? AND supplement_operation_id IS NULL",
                    (operation_id, payload_hash, self._now(), request_id),
                )
            except sqlite3.IntegrityError as exc:
                raise CfazHistoryError("Operação de suplemento duplicada no histórico CFAZ.") from exc
        if connection is not None:
            execute(connection)
            return
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            execute(db)

    def get_record(self, identifier: str) -> CfazHistoryRecord | None:
        with self._connect() as db:
            rows = db.execute(
                """SELECT request_id,provider,patient_name,status,started_at,
                completed_at,duration_seconds,onedrive_destination,provider_exam_id,
                acquisition_sha,import_timestamp,provider_request_id,sequential_id,
                clinic_number,repaired_at,repair_version,supplement_operation_id,
                supplement_payload_hash FROM cfaz_import_history
                WHERE provider='cfaz' AND
                (request_id=? OR provider_request_id=? OR sequential_id=?)""",
                (identifier, identifier, identifier),
            ).fetchall()
        return CfazHistoryRecord(**dict(rows[0])) if len(rows) == 1 else None

    def verify_supplement(self, *, request_id: str, operation_id: str,
                          payload_hash: str, connection: sqlite3.Connection | None = None) -> bool:
        if connection is not None:
            row = connection.execute(
                "SELECT provider,request_id,status,supplement_operation_id,supplement_payload_hash FROM cfaz_import_history "
                "WHERE provider='cfaz' AND request_id=?", (request_id,)
            ).fetchone()
        else:
            with self._connect() as db:
                row = db.execute(
                    "SELECT provider,request_id,status,supplement_operation_id,supplement_payload_hash FROM cfaz_import_history "
                    "WHERE provider='cfaz' AND request_id=?", (request_id,)
                ).fetchone()
        return bool(row and row["provider"] == "cfaz" and row["request_id"] == request_id
                    and row["status"] == "COMPLETE"
                    and row["supplement_operation_id"] == operation_id
                    and row["supplement_payload_hash"] == payload_hash)

    def _update_terminal(
        self, request_id: str, patient_name: str | None, status: str,
        duration: float, destination: str | None, error: str | None,
        provider_exam_id: str | None, acquisition_sha: str | None,
        provider_request_id: str | None, sequential_id: str | None,
        clinic_number: str | None,
    ) -> None:
        now = self._now()
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE cfaz_import_history SET patient_name=COALESCE(?,patient_name),
                provider_exam_id=COALESCE(?,provider_exam_id),
                provider_request_id=COALESCE(?,provider_request_id),
                sequential_id=COALESCE(?,sequential_id),
                clinic_number=COALESCE(?,clinic_number),
                acquisition_sha=COALESCE(?,acquisition_sha),
                status=?,completed_at=?,duration_seconds=?,onedrive_destination=?,
                import_timestamp=CASE WHEN ?='COMPLETE' THEN ? ELSE import_timestamp END,
                error_code=?,updated_at=? WHERE provider='cfaz' AND request_id=?""",
                (patient_name, provider_exam_id, provider_request_id, sequential_id,
                 clinic_number, acquisition_sha, status, now,
                 max(0.0, duration), destination, status, now, error, now, request_id),
            )
            if cursor.rowcount != 1:
                self._upsert(
                    request_id=request_id, patient_name=patient_name,
                    provider_exam_id=provider_exam_id,
                    provider_request_id=provider_request_id,
                    sequential_id=sequential_id, clinic_number=clinic_number,
                    acquisition_sha=acquisition_sha,
                    import_timestamp=now if status == "COMPLETE" else None,
                    status=status, started_at=None, completed_at=now,
                    duration_seconds=max(0.0, duration),
                    onedrive_destination=destination, error_code=error,
                    connection=db,
                )

    def _upsert(
        self, *, request_id: str, patient_name: str | None, status: str,
        started_at: str | None, completed_at: str | None,
        duration_seconds: float | None, onedrive_destination: str | None,
        error_code: str | None, message_id_hash: str | None = None,
        provider_exam_id: str | None = None,
        provider_request_id: str | None = None,
        sequential_id: str | None = None, clinic_number: str | None = None,
        acquisition_sha: str | None = None,
        import_timestamp: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        def execute(db: sqlite3.Connection) -> None:
            db.execute(
                """INSERT INTO cfaz_import_history(
                provider,request_id,provider_request_id,sequential_id,clinic_number,
                provider_exam_id,acquisition_sha,import_timestamp,
                message_id_hash,patient_name,status,started_at,
                completed_at,duration_seconds,onedrive_destination,error_code,updated_at)
                VALUES('cfaz',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(provider,request_id) DO UPDATE SET
                provider_request_id=COALESCE(excluded.provider_request_id,cfaz_import_history.provider_request_id),
                sequential_id=COALESCE(excluded.sequential_id,cfaz_import_history.sequential_id),
                clinic_number=COALESCE(excluded.clinic_number,cfaz_import_history.clinic_number),
                provider_exam_id=COALESCE(excluded.provider_exam_id,cfaz_import_history.provider_exam_id),
                acquisition_sha=COALESCE(excluded.acquisition_sha,cfaz_import_history.acquisition_sha),
                import_timestamp=COALESCE(excluded.import_timestamp,cfaz_import_history.import_timestamp),
                message_id_hash=COALESCE(excluded.message_id_hash,cfaz_import_history.message_id_hash),
                patient_name=COALESCE(excluded.patient_name,cfaz_import_history.patient_name),
                status=excluded.status,started_at=COALESCE(excluded.started_at,cfaz_import_history.started_at),
                completed_at=excluded.completed_at,duration_seconds=excluded.duration_seconds,
                onedrive_destination=excluded.onedrive_destination,error_code=excluded.error_code,
                updated_at=excluded.updated_at""",
                (request_id, provider_request_id, sequential_id, clinic_number,
                 provider_exam_id, acquisition_sha, import_timestamp,
                 message_id_hash, patient_name, status, started_at,
                 completed_at, duration_seconds, onedrive_destination, error_code,
                 self._now()),
            )
        if connection is not None:
            execute(connection)
        else:
            with self._connect() as db:
                execute(db)

    def list_records(self, limit: int = 100) -> list[CfazHistoryRecord]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT request_id,provider,patient_name,status,started_at,
                completed_at,duration_seconds,onedrive_destination,
                provider_exam_id,acquisition_sha,import_timestamp
                ,provider_request_id,sequential_id,clinic_number
                ,repaired_at,repair_version,supplement_operation_id,supplement_payload_hash
                FROM cfaz_import_history ORDER BY updated_at DESC LIMIT ?""",
                (min(max(int(limit), 1), 500),),
            )
            return [CfazHistoryRecord(**dict(row)) for row in rows]

    def list_completed(self) -> list[CfazHistoryRecord]:
        """Lista aquisições Cfaz explícitas concluídas, sem inferir pelo acervo."""
        with self._connect() as db:
            rows = db.execute(
                """SELECT request_id,provider,patient_name,status,started_at,
                completed_at,duration_seconds,onedrive_destination,
                provider_exam_id,acquisition_sha,import_timestamp,
                provider_request_id,sequential_id,clinic_number,
                repaired_at,repair_version,supplement_operation_id,supplement_payload_hash
                FROM cfaz_import_history
                WHERE provider='cfaz' AND status='COMPLETE'
                ORDER BY updated_at,request_id"""
            )
            return [CfazHistoryRecord(**dict(row)) for row in rows]


class CfazNotificationCatalog:
    def __init__(self, *, gmail, history: CfazHistoryRepository, query: str, limit: int):
        self.gmail = gmail
        self.history = history
        self.query = query
        self.limit = limit
        self.diagnostics: list[CfazNotification] = []
        self.counters = CfazNotificationCounters(0, 0, 0, 0)

    def list(self) -> list[CfazNotification]:
        loader = getattr(self.gmail, "list_provider_notifications", None)
        messages = (
            loader(query=self.query, max_results=self.limit)
            if callable(loader) else self.gmail.list_messages(
                query=self.query, max_results=self.limit
            )
        )
        notifications: list[CfazNotification] = []
        diagnostics: list[CfazNotification] = []
        seen: set[str] = set()
        for message in sorted(messages, key=lambda item: item.received_at):
            sender_addresses = {
                address.casefold() for _, address in getaddresses([message.sender])
            }
            sender_ok = "noreply@cfaz.net" in sender_addresses
            subject_ok = bool(re.match(
                r"\s*cfazpost\s*-", message.subject or "", re.IGNORECASE
            ))
            request_ids = CfazProvider.notification_request_ids(message)
            link_found = bool(request_ids)
            request_id = next(iter(request_ids)) if len(request_ids) == 1 else None
            reasons = []
            if not sender_ok:
                reasons.append("remetente não reconhecido")
            if not subject_ok:
                reasons.append("assunto fora do padrão CfazPost")
            if not link_found:
                reasons.append("link de pedido não encontrado")
            elif request_id is None:
                reasons.append("mais de um pedido encontrado")
            accepted = sender_ok and subject_ok and request_id is not None
            item = CfazNotification(
                message=message,
                request_id=request_id,
                patient_name=CfazProvider.notification_patient_name(message),
                received_at=message.received_at,
                imported=bool(request_id and self.history.is_imported(request_id)),
                accepted=accepted,
                rejection_reason="; ".join(reasons) or None,
                subject_recognized=subject_ok,
                link_found=link_found,
            )
            diagnostics.append(item)
            # Uma notificação Cfaz reconhecida sem pedido permanece visível para
            # diagnóstico operacional, mas nunca entra na fila de importação.
            if sender_ok and subject_ok:
                if request_id and request_id in seen:
                    continue
                if request_id:
                    seen.add(request_id)
                notifications.append(item)
        self.diagnostics = diagnostics
        self.counters = CfazNotificationCounters(
            gmail_messages=len(messages),
            cfazpost_subjects=sum(item.subject_recognized for item in diagnostics),
            recognized_links=sum(item.link_found for item in diagnostics),
            operational_notifications=sum(item.accepted for item in diagnostics),
        )
        return notifications

    @staticmethod
    def pending(values: Iterable[CfazNotification]) -> list[CfazNotification]:
        return [item for item in values if item.accepted and not item.imported]
