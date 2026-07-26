"""Testes da associação conservadora DICOM/Clinicorp."""

from __future__ import annotations

from models.patient import Patient
from radiology.dicom_reader import DicomStudy
from radiology.patient_matcher import MatchStatus, PatientMatcher
from tests.synthetic_fixtures import (
    SYNTHETIC_BIRTH_DATE,
    SYNTHETIC_NUMERIC_PATIENT_ID,
    SYNTHETIC_PERSON_ACCENTED,
    SYNTHETIC_PERSON_ACCENTED_ASCII,
    SYNTHETIC_PERSON_DICOM,
    SYNTHETIC_PERSON_EXACT,
    SYNTHETIC_PERSON_EXACT_TITLE,
    SYNTHETIC_PERSON_SIMILAR,
    SYNTHETIC_PERSON_UNRELATED,
    SYNTHETIC_STUDY_DATE,
)


def study(
    *,
    patient_name: str = SYNTHETIC_PERSON_EXACT,
    patient_id: str | None = None,
    birth_date: str | None = None,
) -> DicomStudy:
    """Cria um estudo mínimo para comparação."""

    return DicomStudy(
        patient_name=patient_name,
        patient_id=patient_id,
        study_date=SYNTHETIC_STUDY_DATE,
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
    expected = patient(int(SYNTHETIC_NUMERIC_PATIENT_ID), SYNTHETIC_PERSON_UNRELATED)

    result = PatientMatcher.match(
        study(patient_name=SYNTHETIC_PERSON_EXACT, patient_id=SYNTHETIC_NUMERIC_PATIENT_ID),
        [expected, patient(900002, SYNTHETIC_PERSON_EXACT)],
    )

    assert result.status is MatchStatus.EXACT
    assert result.selected_patient is expected
    assert result.score == 1.0
    assert "PatientID exato" in result.candidates[0].reasons


def test_matches_exact_name_and_birth_date() -> None:
    expected = patient(1, SYNTHETIC_PERSON_EXACT_TITLE, "2090-01-02")

    result = PatientMatcher.match(
        study(patient_name=SYNTHETIC_PERSON_EXACT, birth_date=SYNTHETIC_BIRTH_DATE),
        [expected],
    )

    assert result.status is MatchStatus.EXACT
    assert result.selected_patient is expected
    assert result.reasons == ("Nome e data de nascimento exatos.",)


def test_matches_inverted_dicom_name_with_birth_date() -> None:
    expected = patient(1, SYNTHETIC_PERSON_ACCENTED, "02/01/2090")

    result = PatientMatcher.match(
        study(patient_name=SYNTHETIC_PERSON_DICOM, birth_date=SYNTHETIC_BIRTH_DATE),
        [expected],
    )

    assert result.status is MatchStatus.EXACT
    assert result.selected_patient is expected
    assert any("reordenação DICOM" in reason for reason in result.candidates[0].reasons)


def test_normalizes_accents_and_case() -> None:
    expected = patient(1, SYNTHETIC_PERSON_ACCENTED, "2090-01-02")

    result = PatientMatcher.match(
        study(patient_name=SYNTHETIC_PERSON_ACCENTED_ASCII, birth_date=SYNTHETIC_BIRTH_DATE),
        [expected],
    )

    assert result.status is MatchStatus.EXACT
    assert result.selected_patient is expected


def test_homonyms_are_ambiguous() -> None:
    result = PatientMatcher.match(
        study(patient_name=SYNTHETIC_PERSON_EXACT, birth_date=SYNTHETIC_BIRTH_DATE),
        [
            patient(1, SYNTHETIC_PERSON_EXACT_TITLE, "2090-01-02"),
            patient(2, SYNTHETIC_PERSON_EXACT_TITLE, "2090-01-02"),
        ],
    )

    assert result.status is MatchStatus.AMBIGUOUS
    assert result.selected_patient is None
    assert len(result.candidates) == 2


def test_similar_name_without_birth_never_matches_automatically() -> None:
    result = PatientMatcher.match(
        study(patient_name=SYNTHETIC_PERSON_SIMILAR),
        [patient(1, SYNTHETIC_PERSON_EXACT)],
    )

    assert result.status is MatchStatus.REVIEW_REQUIRED
    assert result.selected_patient is None
    assert result.requires_manual_review is True
    assert any("nome semelhante" in reason for reason in result.candidates[0].reasons)


def test_returns_no_match_without_candidates() -> None:
    result = PatientMatcher.match(
        study(patient_name=SYNTHETIC_PERSON_EXACT),
        [patient(99, SYNTHETIC_PERSON_UNRELATED, "2090-01-02")],
    )

    assert result.status is MatchStatus.NO_MATCH
    assert result.score == 0.0
    assert result.candidates == ()
    assert result.selected_patient is None
