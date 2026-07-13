"""Plano sem efeitos colaterais para o Radiology Intake."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class RadiologyIntakePlan:
    """Descreve uma importação proposta, sem executá-la."""

    message_id: str
    transfer_url: str
    archive_name: Optional[str]
    patient_name_candidate: Optional[str]
    sender_email: Optional[str]
    proposed_destination: str
    requires_manual_review: bool
    review_reasons: list[str]
    status: str
