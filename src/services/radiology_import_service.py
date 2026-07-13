"""Criação de planos de Radiology Intake sem efeitos colaterais."""

from email.utils import parseaddr
import re
from typing import Optional

from integrations.transfernow_connector import TransferNowConnector
from models.email_message import EmailMessage
from models.imaging_exam import ImagingExam
from models.radiology_intake_plan import RadiologyIntakePlan
from models.resolved_patient import ResolvedPatient, ResolutionReason
from observability.audit_logger import (
    AuditEventType,
    AuditLogger,
    emit_safely,
)
from services.patient_resolver import PatientResolver


class RadiologyImportService:
    """Interpreta mensagens e produz planos idempotentes somente em memória."""

    def __init__(self, audit_logger: AuditLogger | None = None) -> None:
        self._plans_by_message_id: dict[str, RadiologyIntakePlan] = {}
        self.audit_logger = audit_logger or AuditLogger()

    @property
    def planned_message_count(self) -> int:
        """Quantidade de mensagens únicas planejadas nesta instância."""

        return len(self._plans_by_message_id)

    def create_plan(
        self,
        message: EmailMessage,
        patient_resolver: PatientResolver,
    ) -> RadiologyIntakePlan:
        """Cria um plano dry-run sem rede, download ou acesso a arquivos."""

        message_id = message.message_id.strip()
        if not message_id:
            raise ValueError("EmailMessage.message_id é obrigatório.")

        existing_plan = self._plans_by_message_id.get(message_id)
        if existing_plan is not None:
            return existing_plan

        content = "\n".join(
            body
            for body in (message.text_body, message.html_body)
            if body
        )
        if not TransferNowConnector.eh_mensagem_transfernow(
            content,
            message.subject,
        ):
            raise ValueError(
                "A mensagem não contém um link HTTPS válido do TransferNow."
            )

        transfer_message = TransferNowConnector.interpretar(
            content,
            message.subject,
        )
        emit_safely(
            self.audit_logger,
            AuditEventType.TRANSFERNOW_MESSAGE_PARSED,
            status="PARSED",
            message_id=message_id,
            archive_name=transfer_message.filename,
            download_url=transfer_message.download_url,
        )
        sender_email = (
            transfer_message.sender_email
            or self._extract_email(message.reply_to)
            or self._extract_email(message.sender)
        )

        exam = ImagingExam(
            patient_name=transfer_message.patient_name_candidate or "",
            source="TransferNow",
            sender_name=parseaddr(message.reply_to or message.sender)[0] or None,
            sender_email=sender_email,
            archive_name=transfer_message.filename,
            received_at=message.received_at,
        )
        resolved_patient = patient_resolver.resolve(exam)
        if resolved_patient.matched:
            exam.patient_id = resolved_patient.patient_id
            exam.patient_name = resolved_patient.patient_name or exam.patient_name

        review_reasons = self._review_reasons(
            resolved_patient,
            transfer_message.filename,
            sender_email,
        )
        requires_manual_review = bool(review_reasons)
        proposed_destination = self._propose_destination(
            exam,
            resolved_patient.patient_name,
            requires_manual_review,
        )

        plan = RadiologyIntakePlan(
            message_id=message_id,
            transfer_url=transfer_message.download_url,
            archive_name=transfer_message.filename,
            patient_name_candidate=transfer_message.patient_name_candidate,
            sender_email=sender_email,
            proposed_destination=proposed_destination,
            requires_manual_review=requires_manual_review,
            review_reasons=review_reasons,
            status=(
                "DRY_RUN_REVIEW_REQUIRED"
                if requires_manual_review
                else "DRY_RUN_READY"
            ),
        )
        self._plans_by_message_id[message_id] = plan
        return plan

    @staticmethod
    def _extract_email(value: Optional[str]) -> Optional[str]:
        if not value:
            return None

        address = parseaddr(value)[1].strip()
        return address or None

    @staticmethod
    def _review_reasons(
        resolved_patient: ResolvedPatient,
        archive_name: Optional[str],
        sender_email: Optional[str],
    ) -> list[str]:
        reasons = []
        resolution_messages = {
            ResolutionReason.MULTIPLE_HIGH_SCORE: (
                "Correspondência ambígua entre pacientes."
            ),
            ResolutionReason.LOW_SCORE: (
                "Correspondência abaixo do limiar automático."
            ),
            ResolutionReason.PATIENT_NOT_FOUND: (
                "Nenhum paciente disponível para comparação."
            ),
            ResolutionReason.PATIENT_SOURCE_UNAVAILABLE: (
                "Fonte de pacientes indisponível; revisão manual obrigatória."
            ),
            ResolutionReason.MANUAL_REVIEW_REQUIRED: (
                "Nome do paciente não identificado."
            ),
        }
        resolution_message = resolution_messages.get(
            resolved_patient.resolution_reason
        )
        if resolution_message:
            reasons.append(resolution_message)
        if not archive_name:
            reasons.append("Nome do arquivo compactado não identificado.")
        if not sender_email:
            reasons.append("E-mail do remetente não identificado.")
        return reasons

    @classmethod
    def _propose_destination(
        cls,
        exam: ImagingExam,
        selected_patient_name: Optional[str],
        requires_manual_review: bool,
    ) -> str:
        patient_segment = (
            selected_patient_name
            if not requires_manual_review
            else "REVIEW_REQUIRED"
        )
        archive_segment = exam.archive_name or "archive-name-pending"
        return "/".join(
            (
                "Radiology Intake",
                cls._safe_segment(patient_segment or "REVIEW_REQUIRED"),
                cls._safe_segment(archive_segment),
            )
        )

    @staticmethod
    def _safe_segment(value: str) -> str:
        """Evita que a proposta textual contenha separadores de caminho."""

        sanitized = re.sub(r"[\\/:*?\"<>|]+", "-", value)
        return re.sub(r"\s+", " ", sanitized).strip(" .") or "pending"
