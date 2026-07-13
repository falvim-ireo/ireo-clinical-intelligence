"""Associação determinística de nomes mantidos somente em memória."""

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional, Sequence

from services.patient_normalizer import PatientNormalizer


@dataclass(frozen=True)
class PatientMatchCandidate:
    """Paciente candidato e sua similaridade com o nome recebido."""

    name: str
    score: float


@dataclass(frozen=True)
class PatientMatchResult:
    """Resultado da comparação, incluindo a decisão conservadora."""

    score: float
    candidates: tuple[PatientMatchCandidate, ...]
    selected_name: Optional[str]
    requires_manual_review: bool
    review_reason: Optional[str]


class PatientMatcher:
    """Compara nomes sem consultar cadastros ou serviços externos."""

    MINIMUM_AUTOMATIC_SCORE = 0.90
    AMBIGUITY_MARGIN = 0.10

    @classmethod
    def match(
        cls,
        patient_name_candidate: Optional[str],
        available_patient_names: Sequence[str],
    ) -> PatientMatchResult:
        """Ordena candidatos e só seleciona uma correspondência forte e única."""

        normalized_candidate = PatientNormalizer.compare_ready(
            patient_name_candidate or ""
        )
        if not normalized_candidate:
            return PatientMatchResult(
                score=0.0,
                candidates=(),
                selected_name=None,
                requires_manual_review=True,
                review_reason="Nome do paciente não identificado.",
            )

        scored_candidates = []
        for patient_name in available_patient_names:
            normalized_patient = PatientNormalizer.compare_ready(patient_name)
            if not normalized_patient:
                continue

            score = round(
                SequenceMatcher(
                    None,
                    normalized_candidate,
                    normalized_patient,
                ).ratio(),
                4,
            )
            scored_candidates.append(
                (
                    PatientMatchCandidate(
                        name=patient_name,
                        score=score,
                    ),
                    normalized_patient,
                )
            )

        scored_candidates.sort(
            key=lambda item: (
                -item[0].score,
                item[1],
                item[0].name.casefold(),
            )
        )
        candidates = tuple(item[0] for item in scored_candidates)

        if not candidates:
            return PatientMatchResult(
                score=0.0,
                candidates=(),
                selected_name=None,
                requires_manual_review=True,
                review_reason="Nenhum paciente disponível para comparação.",
            )

        best = candidates[0]
        if best.score < cls.MINIMUM_AUTOMATIC_SCORE:
            return PatientMatchResult(
                score=best.score,
                candidates=candidates,
                selected_name=None,
                requires_manual_review=True,
                review_reason="Correspondência abaixo do limiar automático.",
            )

        ambiguous = (
            len(candidates) > 1
            and candidates[1].score >= cls.MINIMUM_AUTOMATIC_SCORE
            and best.score - candidates[1].score < cls.AMBIGUITY_MARGIN
        )
        if ambiguous:
            return PatientMatchResult(
                score=best.score,
                candidates=candidates,
                selected_name=None,
                requires_manual_review=True,
                review_reason="Correspondência ambígua entre pacientes.",
            )

        return PatientMatchResult(
            score=best.score,
            candidates=candidates,
            selected_name=best.name,
            requires_manual_review=False,
            review_reason=None,
        )
