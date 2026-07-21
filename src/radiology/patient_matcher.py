"""Associação conservadora entre metadados DICOM e pacientes Clinicorp."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum
import re
import unicodedata

from models.patient import Patient
from radiology.dicom_reader import DicomStudy


class MatchStatus(str, Enum):
    """Estados possíveis de uma decisão de associação."""

    EXACT = "EXACT"
    MATCHED = "MATCHED"
    AMBIGUOUS = "AMBIGUOUS"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    NO_MATCH = "NO_MATCH"


@dataclass(frozen=True)
class PatientMatchCandidate:
    """Paciente Clinicorp pontuado e evidências usadas na comparação."""

    patient: Patient
    score: float
    reasons: tuple[str, ...]
    patient_id_exact: bool = False
    name_exact: bool = False
    birth_date_exact: bool = False

    @property
    def name(self) -> str:
        """Mantém compatibilidade com consumidores que exibem apenas o nome."""

        return self.patient.nome


@dataclass(frozen=True)
class PatientMatchResult:
    """Decisão final, candidatos ordenados e motivos auditáveis."""

    status: MatchStatus
    score: float
    candidates: tuple[PatientMatchCandidate, ...]
    selected_patient: Patient | None
    reasons: tuple[str, ...]

    @property
    def selected_name(self) -> str | None:
        """Retorna o nome selecionado para compatibilidade com o fluxo atual."""

        return self.selected_patient.nome if self.selected_patient else None

    @property
    def requires_manual_review(self) -> bool:
        """Indica que o resultado não autoriza associação automática."""

        return self.status not in {MatchStatus.EXACT, MatchStatus.MATCHED}

    @property
    def review_reason(self) -> str | None:
        """Retorna o primeiro motivo quando há revisão ou ausência de match."""

        return self.reasons[0] if self.requires_manual_review and self.reasons else None


class PatientMatcher:
    """Compara identificador, nome e nascimento sem associação aproximada automática."""

    MINIMUM_NAME_SIMILARITY = 0.65
    TIE_MARGIN = 0.01

    @staticmethod
    def normalize_name(name: str) -> str:
        """Remove acentos, caixa, pontuação e separadores DICOM ``^``."""

        decomposed = unicodedata.normalize("NFKD", str(name or ""))
        without_accents = "".join(
            character
            for character in decomposed
            if not unicodedata.combining(character)
        )
        with_spaces = without_accents.replace("^", " ")
        alphanumeric = re.sub(r"[^A-Za-z0-9]+", " ", with_spaces)
        return re.sub(r"\s+", " ", alphanumeric).strip().casefold()

    @classmethod
    def match(
        cls,
        study: DicomStudy | str | None,
        patients: Collection[Patient] | Sequence[str],
    ) -> PatientMatchResult:
        """Compara um estudo aos pacientes ou atende a chamada legada por nomes."""

        if not isinstance(study, DicomStudy):
            return cls._legacy_name_match(study, patients)
        clinicorp_patients = tuple(
            patient for patient in patients if isinstance(patient, Patient)
        )
        if not clinicorp_patients:
            return cls._result(
                MatchStatus.NO_MATCH,
                (),
                None,
                "Nenhum paciente Clinicorp disponível para comparação.",
            )

        candidates = tuple(
            sorted(
                (
                    cls._score_patient(study, patient)
                    for patient in clinicorp_patients
                ),
                key=lambda candidate: (
                    -candidate.score,
                    cls.normalize_name(candidate.patient.nome),
                    str(candidate.patient.id),
                ),
            )
        )
        candidates = tuple(
            candidate
            for candidate in candidates
            if candidate.patient_id_exact
            or candidate.name_exact
            or any(
                reason.startswith("nome semelhante")
                for reason in candidate.reasons
            )
        )
        if not candidates:
            return cls._result(
                MatchStatus.NO_MATCH,
                (),
                None,
                "Nenhum paciente compatível foi encontrado.",
            )

        id_matches = tuple(candidate for candidate in candidates if candidate.patient_id_exact)
        if len(id_matches) == 1:
            selected = id_matches[0]
            return PatientMatchResult(
                MatchStatus.EXACT,
                selected.score,
                candidates,
                selected.patient,
                ("PatientID exato e único.",),
            )
        if len(id_matches) > 1:
            return cls._result(
                MatchStatus.AMBIGUOUS,
                candidates,
                None,
                "PatientID corresponde a mais de um paciente.",
            )

        confirmed = tuple(
            candidate
            for candidate in candidates
            if candidate.name_exact and candidate.birth_date_exact
        )
        if len(confirmed) == 1:
            selected = confirmed[0]
            return PatientMatchResult(
                MatchStatus.EXACT,
                selected.score,
                candidates,
                selected.patient,
                ("Nome e data de nascimento exatos.",),
            )
        if len(confirmed) > 1:
            return cls._result(
                MatchStatus.AMBIGUOUS,
                candidates,
                None,
                "Nome e nascimento correspondem a mais de um paciente.",
            )

        best = candidates[0]
        tied = tuple(
            candidate
            for candidate in candidates
            if best.score - candidate.score <= cls.TIE_MARGIN
        )
        if len(tied) > 1:
            return cls._result(
                MatchStatus.AMBIGUOUS,
                candidates,
                None,
                "Empate entre candidatos; associação automática bloqueada.",
            )
        return cls._result(
            MatchStatus.REVIEW_REQUIRED,
            candidates,
            None,
            "Evidência insuficiente para associação automática.",
        )

    @classmethod
    def _score_patient(
        cls, study: DicomStudy, patient: Patient
    ) -> PatientMatchCandidate:
        """Calcula a pontuação e as evidências de um paciente."""

        dicom_id = cls._normalize_id(study.patient_id)
        clinicorp_id = cls._normalize_id(patient.id)
        id_exact = bool(dicom_id and clinicorp_id and dicom_id == clinicorp_id)

        dicom_name = cls.normalize_name(study.patient_name or "")
        clinicorp_name = cls.normalize_name(patient.nome)
        direct_name_exact = bool(dicom_name and dicom_name == clinicorp_name)
        reordered_name_exact = bool(
            dicom_name
            and clinicorp_name
            and sorted(dicom_name.split()) == sorted(clinicorp_name.split())
        )
        name_exact = direct_name_exact or reordered_name_exact
        similarity = cls._name_similarity(dicom_name, clinicorp_name)

        dicom_birth = cls._normalize_date(study.patient_birth_date)
        clinicorp_birth = cls._normalize_date(patient.data_nascimento)
        birth_exact = bool(
            dicom_birth and clinicorp_birth and dicom_birth == clinicorp_birth
        )

        reasons: list[str] = []
        if id_exact:
            reasons.append("PatientID exato")
        if direct_name_exact:
            reasons.append("nome exato")
        elif reordered_name_exact:
            reasons.append("nome exato após reordenação DICOM")
        elif similarity >= cls.MINIMUM_NAME_SIMILARITY:
            reasons.append(f"nome semelhante ({similarity:.2f})")
        if birth_exact:
            reasons.append("data de nascimento exata")

        if id_exact:
            score = 1.0
        elif name_exact:
            score = 1.0 if birth_exact else 0.70
        else:
            score = min(0.90, similarity * 0.65 + (0.25 if birth_exact else 0.0))
        return PatientMatchCandidate(
            patient=patient,
            score=round(score, 4),
            reasons=tuple(reasons),
            patient_id_exact=id_exact,
            name_exact=name_exact,
            birth_date_exact=birth_exact,
        )

    @classmethod
    def _name_similarity(cls, left: str, right: str) -> float:
        """Compara nomes na ordem original e com tokens ordenados."""

        if not left or not right:
            return 0.0
        direct = SequenceMatcher(None, left, right).ratio()
        reordered = SequenceMatcher(
            None, " ".join(sorted(left.split())), " ".join(sorted(right.split()))
        ).ratio()
        return max(direct, reordered)

    @staticmethod
    def _normalize_id(value: object) -> str:
        """Normaliza IDs textuais e numéricos, preservando conteúdo alfanumérico."""

        normalized = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).casefold()
        if normalized.isdigit():
            return normalized.lstrip("0") or "0"
        return normalized

    @staticmethod
    def _normalize_date(value: str | None) -> str:
        """Normaliza datas DICOM, ISO e brasileiras para ``YYYYMMDD``."""

        digits = re.sub(r"\D", "", str(value or ""))
        if len(digits) != 8:
            return ""
        if digits[:4].isdigit() and 1900 <= int(digits[:4]) <= 2200:
            return digits
        return f"{digits[4:]}{digits[2:4]}{digits[:2]}"

    @staticmethod
    def _result(
        status: MatchStatus,
        candidates: tuple[PatientMatchCandidate, ...],
        selected: Patient | None,
        reason: str,
    ) -> PatientMatchResult:
        """Constrói um resultado sem repetir regras de score e motivo."""

        return PatientMatchResult(
            status=status,
            score=candidates[0].score if candidates else 0.0,
            candidates=candidates,
            selected_patient=selected,
            reasons=(reason,),
        )

    @classmethod
    def _legacy_name_match(
        cls,
        name: str | None,
        patients: Collection[Patient] | Sequence[str],
    ) -> PatientMatchResult:
        """Preserva o comportamento antigo para consumidores ainda baseados em nomes."""

        normalized = cls.normalize_name(name or "")
        legacy_patients = tuple(
            patient
            if isinstance(patient, Patient)
            else Patient(id=index, nome=str(patient))
            for index, patient in enumerate(patients, start=1)
        )
        if not normalized or not legacy_patients:
            return cls._result(
                MatchStatus.NO_MATCH,
                (),
                None,
                "Nome do paciente não identificado ou lista vazia.",
            )
        candidates = tuple(
            sorted(
                (
                    PatientMatchCandidate(
                        patient=patient,
                        score=round(
                            cls._name_similarity(
                                normalized, cls.normalize_name(patient.nome)
                            ),
                            4,
                        ),
                        reasons=("comparação legada por nome",),
                    )
                    for patient in legacy_patients
                ),
                key=lambda candidate: (-candidate.score, candidate.name.casefold()),
            )
        )
        best = candidates[0]
        if best.score < 0.90:
            return cls._result(
                MatchStatus.REVIEW_REQUIRED,
                candidates,
                None,
                "Correspondência abaixo do limiar automático.",
            )
        if len(candidates) > 1 and best.score - candidates[1].score < 0.10:
            return cls._result(
                MatchStatus.AMBIGUOUS,
                candidates,
                None,
                "Correspondência ambígua entre pacientes.",
            )
        return PatientMatchResult(
            MatchStatus.MATCHED,
            best.score,
            candidates,
            best.patient,
            ("Compatibilidade legada: nome forte e único.",),
        )
