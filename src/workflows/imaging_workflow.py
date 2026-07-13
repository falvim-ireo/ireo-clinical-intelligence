"""Orquestração dry-run do Radiology Intake."""

from typing import Optional

from models.email_message import EmailMessage
from models.radiology_intake_plan import RadiologyIntakePlan
from repositories.patient_repository import EmptyPatientRepository
from services.patient_resolver import PatientResolver
from services.radiology_import_service import RadiologyImportService


class ImagingWorkflow:
    """Produz planos em memória sem executar qualquer operação externa."""

    def __init__(
        self,
        service: Optional[RadiologyImportService] = None,
        patient_resolver: Optional[PatientResolver] = None,
    ) -> None:
        self.service = service or RadiologyImportService()
        self.patient_resolver = patient_resolver or PatientResolver(
            EmptyPatientRepository()
        )

    def run_dry_run(
        self,
        message: EmailMessage,
    ) -> RadiologyIntakePlan:
        """Retorna o plano proposto e não realiza a importação."""

        return self.service.create_plan(message, self.patient_resolver)
