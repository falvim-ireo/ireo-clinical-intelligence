"""Resultado estruturado da resolução de identidade de um paciente."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ResolutionReason(str, Enum):
    """Motivo determinístico para a decisão do resolvedor."""

    EXACT_NAME = "EXACT_NAME"
    NORMALIZED_NAME = "NORMALIZED_NAME"
    SINGLE_HIGH_SCORE = "SINGLE_HIGH_SCORE"
    MULTIPLE_HIGH_SCORE = "MULTIPLE_HIGH_SCORE"
    LOW_SCORE = "LOW_SCORE"
    PATIENT_NOT_FOUND = "PATIENT_NOT_FOUND"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"


@dataclass
class ResolvedPatient:
    """Identidade resolvida ou justificativa para revisão humana."""

    patient_id: Optional[int]
    patient_name: Optional[str]
    matched: bool
    requires_manual_review: bool
    confidence_score: float
    resolution_reason: ResolutionReason
    candidate_count: int
    candidate_names: list[str]
    matched_by: Optional[str]
