"""Testes da associação conservadora DICOM/Clinicorp."""

from __future__ import annotations

from models.patient import Patient
from radiology.dicom_reader import DicomStudy
from radiology.patient_matcher import MatchStatus, PatientMatcher


def study(
    *,
    patient_name: str = "JOAO DA SILVA",
    patient_id: str | None = None,
    birth_date: str | None = None,
) -> DicomStudy:
    """Cria um estudo mínimo para comparação."""

    return DicomStudy(
        patient_name=patient_name,
        patient_id=patient_id,
        study_date="20260721",
        study_description=None,
        study_instance_uid="1.2.3",
        manufacturer=None,
        manufacturer_model_name=None,
        institution_name=None,
        modality="CT",
        series=[],
        patient_birth_date=birth_date,
    )


def patient(identifier: int, name: str, birth_date: str | None = None) -> Patient:
    """Cria um paciente Clinicorp para os cenários de teste."""

    return Patient(id=identifier, nome=name, data_nascimento=birth_date)


def test_matches_exact_patient_id() -> None:
    expected = patient(123, "Nome divergente")

    result = PatientMatcher.match(
        study(patient_name="OUTRO NOME", patient_id="000123"),
        [expected, patient(456, "OUTRO NOME")],
    )

    assert result.status is MatchStatus.EXACT
    assert result.selected_patient is expected
    assert result.score == 1.0
    assert "PatientID exato" in result.candidates[0].reasons


def test_matches_exact_name_and_birth_date() -> None:
    expected = patient(1, "João da Silva", "1980-05-03")

    result = PatientMatcher.match(
        study(patient_name="JOAO DA SILVA", birth_date="19800503"),
        [expected],
    )

    assert result.status is MatchStatus.EXACT
    assert result.selected_patient is expected
    assert result.reasons == ("Nome e data de nascimento exatos.",)


def test_matches_inverted_dicom_name_with_birth_date() -> None:
    expected = patient(1, "João Carlos Silva", "03/05/1980")

    result = PatientMatcher.match(
        study(patient_name="SILVA^JOAO^CARLOS", birth_date="19800503"),
        [expected],
    )

    assert result.status is MatchStatus.EXACT
    assert result.selected_patient is expected
    assert any("reordenação DICOM" in reason for reason in result.candidates[0].reasons)


def test_normalizes_accents_and_case() -> None:
    expected = patient(1, "Cláudia Ângela", "1991-12-30")

    result = PatientMatcher.match(
        study(patient_name="CLAUDIA^ANGELA", birth_date="19911230"),
        [expected],
    )

    assert result.status is MatchStatus.EXACT
    assert result.selected_patient is expected


def test_homonyms_are_ambiguous() -> None:
    result = PatientMatcher.match(
        study(patient_name="MARIA SOUZA", birth_date="19900101"),
        [
            patient(1, "Maria Souza", "1990-01-01"),
            patient(2, "Maria Souza", "1990-01-01"),
        ],
    )

    assert result.status is MatchStatus.AMBIGUOUS
    assert result.selected_patient is None
    assert len(result.candidates) == 2


def test_similar_name_without_birth_never_matches_automatically() -> None:
    result = PatientMatcher.match(
        study(patient_name="JOAO SILVA"),
        [patient(1, "João da Silveira")],
    )

    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert result.selected_patient is None
    assert result.requires_manual_review is True
    assert any("nome semelhante" in reason for reason in result.candidates[0].reasons)


def test_returns_no_match_without_candidates() -> None:
    result = PatientMatcher.match(
        study(patient_name="JOAO DA SILVA"),
        [patient(99, "MARIA OLIVEIRA", "1970-01-01")],
    )

    assert result.status is MatchStatus.NO_MATCH
    assert result.score == 0.0
    assert result.candidates == ()
    assert result.selected_patient is None
