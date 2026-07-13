"""Interface segura para planejar mensagens reais do Gmail em dry-run."""

from collections.abc import Callable
from typing import Optional

from integrations.gmail_connector import GmailConnector
from models.email_message import EmailMessage
from models.radiology_intake_plan import RadiologyIntakePlan
from observability.audit_logger import (
    AuditEventType,
    AuditLogger,
    emit_safely,
)
from workflows.imaging_workflow import ImagingWorkflow


def mask_message_id(message_id: str) -> str:
    """Mascara um identificador sem impedir correlação operacional."""

    value = str(message_id or "").strip()
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def run_gmail_dry_run(
    connector: Optional[GmailConnector] = None,
    workflow: Optional[ImagingWorkflow] = None,
    output: Callable[[str], None] = print,
    audit_logger: Optional[AuditLogger] = None,
) -> int:
    """Lê mensagens e exibe somente os campos permitidos do plano."""

    gmail = connector or GmailConnector()
    imaging_workflow = workflow or ImagingWorkflow()
    audit = (
        audit_logger
        or getattr(imaging_workflow, "audit_logger", None)
        or AuditLogger()
    )

    for message in gmail.list_messages():
        emit_safely(
            audit,
            AuditEventType.RADIOLOGY_EMAIL_DETECTED,
            status="DETECTED",
            message_id=message.message_id,
        )
        try:
            plan = imaging_workflow.run_dry_run(
                message,
            )
        except ValueError:
            _print_rejected(message, output)
            continue

        _print_plan(plan, output)

    return 0


def _print_plan(
    plan: RadiologyIntakePlan,
    output: Callable[[str], None],
) -> None:
    output(f"message_id: {mask_message_id(plan.message_id)}")
    output(f"arquivo: {plan.archive_name or 'não identificado'}")
    output(
        "paciente provável: "
        f"{plan.patient_name_candidate or 'não identificado'}"
    )
    output(f"status do matching: {plan.status}")
    output(
        "revisão necessária: "
        f"{'sim' if plan.requires_manual_review else 'não'}"
    )
    output(f"destino lógico: {plan.proposed_destination}")


def _print_rejected(
    message: EmailMessage,
    output: Callable[[str], None],
) -> None:
    output(f"message_id: {mask_message_id(message.message_id)}")
    output("arquivo: não identificado")
    output("paciente provável: não identificado")
    output("status do matching: REJECTED")
    output("revisão necessária: sim")
    output("destino lógico: não proposto")
