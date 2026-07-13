"""Orquestração dry-run do Radiology Intake."""

from typing import Optional

from models.email_message import EmailMessage
from models.radiology_intake_plan import RadiologyIntakePlan
from observability.audit_logger import (
    AuditEventType,
    AuditLogger,
    emit_safely,
)
from repositories.patient_repository import EmptyPatientRepository
from services.patient_resolver import PatientResolver
from services.radiology_import_service import RadiologyImportService


class ImagingWorkflow:
    """Produz planos em memória sem executar qualquer operação externa."""

    def __init__(
        self,
        service: Optional[RadiologyImportService] = None,
        patient_resolver: Optional[PatientResolver] = None,
        audit_logger: Optional[AuditLogger] = None,
    ) -> None:
        self.audit_logger = (
            audit_logger
            or getattr(patient_resolver, "audit_logger", None)
            or getattr(service, "audit_logger", None)
            or AuditLogger()
        )
        self.service = service or RadiologyImportService(self.audit_logger)
        self.patient_resolver = patient_resolver or PatientResolver(
            EmptyPatientRepository(),
            audit_logger=self.audit_logger,
        )

    def run_dry_run(
        self,
        message: EmailMessage,
    ) -> RadiologyIntakePlan:
        """Retorna o plano proposto e não realiza a importação."""

        try:
            plan = self.service.create_plan(message, self.patient_resolver)
        except Exception:
            emit_safely(
                self.audit_logger,
                AuditEventType.RADIOLOGY_DRY_RUN_FAILED,
                status="FAILED",
                message_id=message.message_id,
                requires_manual_review=True,
                reason_code="DRY_RUN_ERROR",
            )
            raise

        emit_safely(
            self.audit_logger,
            AuditEventType.RADIOLOGY_DRY_RUN_COMPLETED,
            status="COMPLETED",
            message_id=plan.message_id,
            archive_name=plan.archive_name,
            requires_manual_review=plan.requires_manual_review,
            reason_code=plan.status,
        )
        return plan
