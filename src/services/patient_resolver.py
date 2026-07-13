"""Resolução desacoplada e determinística da identidade do paciente."""

from dataclasses import dataclass
from difflib import SequenceMatcher

from models.imaging_exam import ImagingExam
from models.patient import Patient
from models.resolved_patient import ResolvedPatient, ResolutionReason
from repositories.patient_repository import PatientRepository
from services.patient_normalizer import PatientNormalizer


@dataclass(frozen=True)
class _ScoredCandidate:
    patient: Patient
    normalized_name: str
    score: float


class PatientResolver:
    """Resolve pacientes usando apenas candidatos fornecidos por uma porta."""

    MINIMUM_MATCH_SCORE = 0.90

    def __init__(self, repository: PatientRepository) -> None:
        self.repository = repository

    @classmethod
    def similarity_score(cls, first_name: str, second_name: str) -> float:
        """Calcula similaridade estável entre dois nomes normalizados."""

        first = PatientNormalizer.compare_ready(first_name)
        second = PatientNormalizer.compare_ready(second_name)
        if not first or not second:
            return 0.0
        return round(SequenceMatcher(None, first, second).ratio(), 4)

    def resolve(self, exam: ImagingExam) -> ResolvedPatient:
        """Consulta candidatos e retorna uma decisão explicável."""

        candidates = tuple(
            self.repository.find_candidates(exam.patient_name)
        )
        if not candidates:
            return self._unmatched(
                reason=ResolutionReason.PATIENT_NOT_FOUND,
                confidence_score=0.0,
                candidates=(),
            )

        normalized_exam_name = PatientNormalizer.compare_ready(
            exam.patient_name
        )
        scored_candidates = tuple(
            sorted(
                (
                    _ScoredCandidate(
                        patient=patient,
                        normalized_name=PatientNormalizer.compare_ready(
                            patient.nome
                        ),
                        score=self.similarity_score(
                            exam.patient_name,
                            patient.nome,
                        ),
                    )
                    for patient in candidates
                ),
                key=lambda candidate: (
                    -candidate.score,
                    candidate.normalized_name,
                    str(candidate.patient.id),
                ),
            )
        )

        if not normalized_exam_name:
            return self._unmatched(
                reason=ResolutionReason.MANUAL_REVIEW_REQUIRED,
                confidence_score=0.0,
                candidates=scored_candidates,
            )

        eligible = tuple(
            candidate
            for candidate in scored_candidates
            if candidate.score >= self.MINIMUM_MATCH_SCORE
        )
        if len(eligible) > 1:
            return self._unmatched(
                reason=ResolutionReason.MULTIPLE_HIGH_SCORE,
                confidence_score=eligible[0].score,
                candidates=scored_candidates,
            )

        if not eligible:
            return self._unmatched(
                reason=ResolutionReason.LOW_SCORE,
                confidence_score=scored_candidates[0].score,
                candidates=scored_candidates,
            )

        selected = eligible[0]
        if exam.patient_name == selected.patient.nome:
            reason = ResolutionReason.EXACT_NAME
            matched_by = "exact_name"
        elif normalized_exam_name == selected.normalized_name:
            reason = ResolutionReason.NORMALIZED_NAME
            matched_by = "normalized_name"
        else:
            reason = ResolutionReason.SINGLE_HIGH_SCORE
            matched_by = "similarity_score"

        return ResolvedPatient(
            patient_id=selected.patient.id,
            patient_name=selected.patient.nome,
            matched=True,
            requires_manual_review=False,
            confidence_score=selected.score,
            resolution_reason=reason,
            candidate_count=len(scored_candidates),
            candidate_names=self._candidate_names(scored_candidates),
            matched_by=matched_by,
        )

    @classmethod
    def _unmatched(
        cls,
        reason: ResolutionReason,
        confidence_score: float,
        candidates: tuple[_ScoredCandidate, ...],
    ) -> ResolvedPatient:
        return ResolvedPatient(
            patient_id=None,
            patient_name=None,
            matched=False,
            requires_manual_review=True,
            confidence_score=confidence_score,
            resolution_reason=reason,
            candidate_count=len(candidates),
            candidate_names=cls._candidate_names(candidates),
            matched_by=None,
        )

    @staticmethod
    def _candidate_names(
        candidates: tuple[_ScoredCandidate, ...],
    ) -> list[str]:
        return [candidate.patient.nome for candidate in candidates]
