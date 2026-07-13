from datetime import datetime, timezone
import socket

import requests

from models.email_message import EmailMessage
from models.imaging_exam import ImagingExam
from models.patient import Patient
from models.resolved_patient import ResolutionReason
from repositories.patient_repository import InMemoryPatientRepository
from services.patient_resolver import PatientResolver
from workflows.imaging_workflow import ImagingWorkflow


class RecordingPatientRepository(InMemoryPatientRepository):
    def __init__(self, patients: list[Patient]) -> None:
        super().__init__(patients)
        self.queries: list[str] = []

    def find_candidates(self, name: str):
        self.queries.append(name)
        return super().find_candidates(name)


def resolve(name: str, patients: list[Patient]):
    repository = InMemoryPatientRepository(patients)
    return PatientResolver(repository).resolve(ImagingExam(patient_name=name))


def test_exact_name_match_returns_patient_and_explanation() -> None:
    result = resolve(
        "MARIA DA SILVA",
        [Patient(id=101, nome="MARIA DA SILVA")],
    )

    assert result.patient_id == 101
    assert result.patient_name == "MARIA DA SILVA"
    assert result.matched is True
    assert result.requires_manual_review is False
    assert result.confidence_score == 1.0
    assert result.resolution_reason is ResolutionReason.EXACT_NAME
    assert result.candidate_count == 1
    assert result.candidate_names == ["MARIA DA SILVA"]
    assert result.matched_by == "exact_name"


def test_accents_are_ignored_by_normalized_matching() -> None:
    result = resolve("JOSE ALVARES", [Patient(id=102, nome="JOSÉ ÁLVARES")])

    assert result.confidence_score == 1.0
    assert result.resolution_reason is ResolutionReason.NORMALIZED_NAME
    assert result.matched_by == "normalized_name"


def test_letter_case_is_ignored_by_normalized_matching() -> None:
    result = resolve("ANA SOUZA", [Patient(id=103, nome="ana souza")])

    assert result.matched is True
    assert result.resolution_reason is ResolutionReason.NORMALIZED_NAME


def test_dicom_caret_is_converted_to_space() -> None:
    result = resolve(
        "SILVA^JOAO^CARLOS",
        [Patient(id=104, nome="SILVA JOÃO CARLOS")],
    )

    assert result.matched is True
    assert result.confidence_score == 1.0
    assert result.resolution_reason is ResolutionReason.NORMALIZED_NAME


def test_duplicate_spaces_and_special_characters_are_ignored() -> None:
    result = resolve(
        "  MARIA   DA-SILVA  ",
        [Patient(id=105, nome="maria da silva")],
    )

    assert result.matched is True
    assert result.resolution_reason is ResolutionReason.NORMALIZED_NAME


def test_patient_not_found_requires_manual_review() -> None:
    result = resolve("PACIENTE AUSENTE", [])

    assert result.patient_id is None
    assert result.patient_name is None
    assert result.matched is False
    assert result.requires_manual_review is True
    assert result.confidence_score == 0.0
    assert result.resolution_reason is ResolutionReason.PATIENT_NOT_FOUND
    assert result.candidate_count == 0
    assert result.candidate_names == []
    assert result.matched_by is None


def test_multiple_high_scores_never_select_automatically() -> None:
    result = resolve(
        "ANA MARIA SILVA",
        [
            Patient(id=106, nome="ANA MARIA SILVA"),
            Patient(id=107, nome="ANA MARIA DA SILVA"),
        ],
    )

    assert result.patient_id is None
    assert result.patient_name is None
    assert result.matched is False
    assert result.requires_manual_review is True
    assert result.confidence_score == 1.0
    assert result.resolution_reason is ResolutionReason.MULTIPLE_HIGH_SCORE
    assert result.candidate_count == 2
    assert result.candidate_names == [
        "ANA MARIA SILVA",
        "ANA MARIA DA SILVA",
    ]


def test_single_high_score_selects_only_eligible_candidate() -> None:
    result = resolve(
        "MARIA SILV",
        [
            Patient(id=108, nome="MARIA SILVA"),
            Patient(id=109, nome="CARLOS PEREIRA"),
        ],
    )

    assert result.patient_id == 108
    assert result.matched is True
    assert result.confidence_score >= 0.90
    assert result.resolution_reason is ResolutionReason.SINGLE_HIGH_SCORE
    assert result.candidate_count == 2
    assert result.matched_by == "similarity_score"


def test_low_score_requires_manual_review() -> None:
    result = resolve(
        "CARLOS EDUARDO",
        [Patient(id=110, nome="MARIANA COSTA")],
    )

    assert result.patient_id is None
    assert result.matched is False
    assert result.confidence_score < 0.90
    assert result.resolution_reason is ResolutionReason.LOW_SCORE
    assert result.requires_manual_review is True


def test_missing_exam_name_requires_manual_review() -> None:
    result = resolve("", [Patient(id=111, nome="MARIA SILVA")])

    assert result.patient_id is None
    assert result.confidence_score == 0.0
    assert result.resolution_reason is ResolutionReason.MANUAL_REVIEW_REQUIRED
    assert result.requires_manual_review is True


def test_similarity_with_empty_name_is_zero() -> None:
    assert PatientResolver.similarity_score("", "MARIA SILVA") == 0.0


def test_workflow_uses_injected_resolver_without_external_access(
    monkeypatch,
) -> None:
    def forbidden_network(*args, **kwargs):
        raise AssertionError("Acesso externo proibido")

    repository = RecordingPatientRepository(
        [Patient(id=112, nome="JOÃO SILVA")]
    )
    workflow = ImagingWorkflow(
        patient_resolver=PatientResolver(repository)
    )
    message = EmailMessage(
        message_id="resolver-workflow-001",
        subject='TransferNow - "JOAO SILVA_20260713.zip"',
        sender="TransferNow <noreply@transfernow.net>",
        reply_to="Sorrimagem <contato@sorrimagem.example>",
        recipients=["radiologia@ireo.example"],
        received_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
        text_body="Contato: contato@sorrimagem.example",
        html_body=(
            '<a href="https://transfernow.net/dl/test-token">Baixar</a>'
        ),
    )
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden_network)
    monkeypatch.setattr(socket, "create_connection", forbidden_network)

    plan = workflow.run_dry_run(message)

    assert repository.queries == ["JOAO SILVA"]
    assert plan.requires_manual_review is False
    assert plan.status == "DRY_RUN_READY"
    assert "/JOÃO SILVA/" in plan.proposed_destination
