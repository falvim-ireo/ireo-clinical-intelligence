"""Orquestração dry-run do Radiology Intake."""

from typing import Optional, Sequence

from models.email_message import EmailMessage
from models.radiology_intake_plan import RadiologyIntakePlan
from services.radiology_import_service import RadiologyImportService


class ImagingWorkflow:
    """Produz planos em memória sem executar qualquer operação externa."""

    def __init__(
        self,
        service: Optional[RadiologyImportService] = None,
    ) -> None:
        self.service = service or RadiologyImportService()

    def run_dry_run(
        self,
        message: EmailMessage,
        available_patient_names: Sequence[str],
    ) -> RadiologyIntakePlan:
        """Retorna o plano proposto e não realiza a importação."""

        return self.service.create_plan(
            message,
            available_patient_names,
        )
