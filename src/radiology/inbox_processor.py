"""Processamento manual, isolado e idempotente da caixa radiológica."""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from enum import Enum
import hashlib
import logging
from pathlib import Path
from typing import Protocol

from integrations.transfernow_connector import TransferNowConnector, TransferNowMessage
from models.email_message import EmailMessage
from models.patient import Patient
from storage.onedrive_radiology_organizer import (
    OneDriveRadiologyOrganizer,
    OrganizerStatus,
)
from storage.radiology_storage import ExamState, RadiologyStorage, StoredExam
from workflows.radiology_workflow import RadiologyWorkflow


class GmailInbox(Protocol):
    """Operações Gmail estritamente necessárias ao comando."""

    def list_messages(self, query: str | None = None, max_results: int | None = None) -> list[EmailMessage]: ...

    def apply_label(self, message_id: str, label_name: str) -> None: ...


class InboxOutcome(str, Enum):
    """Resultados terminais de uma mensagem."""

    PROCESSED = "PROCESSED"
    DUPLICATE = "DUPLICATE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class InboxProcessingSummary:
    """Contadores seguros da execução manual."""

    found: int
    processed: int
    duplicates: int
    review_required: int
    failed: int


class RadiologyInboxProcessor:
    """Executa o pipeline completo e etiqueta cada mensagem somente ao final."""

    LABELS = {
        InboxOutcome.PROCESSED: "IREO/Radiologia/Processado",
        InboxOutcome.DUPLICATE: "IREO/Radiologia/Duplicado",
        InboxOutcome.REVIEW_REQUIRED: "IREO/Radiologia/Revisar",
        InboxOutcome.FAILED: "IREO/Radiologia/Falha",
    }

    def __init__(
        self,
        *,
        gmail: GmailInbox,
        workflow: RadiologyWorkflow,
        storage: RadiologyStorage,
        organizer: OneDriveRadiologyOrganizer,
        patient_provider: Callable[[TransferNowMessage], Collection[Patient]],
        download_root: str | Path,
        logger: logging.Logger | None = None,
    ) -> None:
        self.gmail = gmail
        self.workflow = workflow
        self.storage = storage
        self.organizer = organizer
        self.patient_provider = patient_provider
        self.download_root = Path(download_root)
        self.logger = logger or logging.getLogger(__name__)

    def run(self, *, max_messages: int = 5) -> InboxProcessingSummary:
        """Busca mensagens pendentes e processa cada uma independentemente."""

        messages = self.gmail.list_messages(
            query=self._pending_query(), max_results=max_messages
        )
        counts = {outcome: 0 for outcome in InboxOutcome}
        for message in messages:
            outcome = self._process_message(message)
            counts[outcome] += 1
            try:
                self.gmail.apply_label(message.message_id, self.LABELS[outcome])
            except Exception as exc:
                self.logger.error(
                    "Resultado concluído, mas label Gmail falhou (%s).",
                    type(exc).__name__,
                )
                if outcome is not InboxOutcome.FAILED:
                    counts[outcome] -= 1
                    counts[InboxOutcome.FAILED] += 1

        return InboxProcessingSummary(
            found=len(messages),
            processed=counts[InboxOutcome.PROCESSED],
            duplicates=counts[InboxOutcome.DUPLICATE],
            review_required=counts[InboxOutcome.REVIEW_REQUIRED],
            failed=counts[InboxOutcome.FAILED],
        )

    def _process_message(self, message: EmailMessage) -> InboxOutcome:
        stored: StoredExam | None = None
        try:
            body = "\n".join(
                part for part in (message.html_body, message.text_body) if part
            )
            transfer = TransferNowConnector.interpretar(body, message.subject)
            patients = self.patient_provider(transfer)
            workflow_result = self.workflow.run(
                transfer_url=transfer.download_url,
                original_filename=transfer.original_filename,
                download_root=self.download_root,
                correlation_id=self._correlation_id(message.message_id),
                message_id=message.message_id,
                patients=patients,
            )
            if workflow_result.download is not None:
                stored = self.storage.register_received(workflow_result.download.path)
                stored = self.storage.move(stored, ExamState.PROCESSING)
            if workflow_result.errors:
                if stored is not None:
                    self.storage.move(stored, ExamState.FAILED)
                return InboxOutcome.FAILED

            organized = self.organizer.organize(workflow_result)
            outcome = self._organizer_outcome(organized.status)
            if stored is not None:
                target = (
                    ExamState.PROCESSED
                    if outcome in {InboxOutcome.PROCESSED, InboxOutcome.DUPLICATE}
                    else ExamState.FAILED
                )
                self.storage.move(stored, target)
            return outcome
        except Exception as exc:
            self.logger.error(
                "Mensagem radiológica falhou isoladamente (%s).",
                type(exc).__name__,
            )
            if stored is not None and stored.state is ExamState.PROCESSING:
                try:
                    self.storage.move(stored, ExamState.FAILED)
                except Exception:
                    self.logger.error("Não foi possível preservar o estado de falha.")
            return InboxOutcome.FAILED

    @classmethod
    def _pending_query(cls) -> str:
        exclusions = " ".join(
            f'-label:"{label}"' for label in cls.LABELS.values()
        )
        return f"from:transfernow.net subject:TransferNow {exclusions}"

    @staticmethod
    def _correlation_id(message_id: str) -> str:
        return hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _organizer_outcome(status: OrganizerStatus) -> InboxOutcome:
        return {
            OrganizerStatus.UPLOADED: InboxOutcome.PROCESSED,
            OrganizerStatus.DUPLICATE: InboxOutcome.DUPLICATE,
            OrganizerStatus.REVIEW_REQUIRED: InboxOutcome.REVIEW_REQUIRED,
            OrganizerStatus.FAILED: InboxOutcome.FAILED,
        }[status]
