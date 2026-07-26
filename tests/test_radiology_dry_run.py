from datetime import datetime, timezone
from pathlib import Path
import socket

import pytest
import requests

from models.email_message import EmailMessage
from models.patient import Patient
from radiology.patient_matcher import PatientMatcher
from repositories.patient_repository import InMemoryPatientRepository
from services.patient_resolver import PatientResolver
from tests.synthetic_fixtures import (
    SYNTHETIC_ARCHIVE_NAME,
    SYNTHETIC_PERSON_ACCENTED,
    SYNTHETIC_PERSON_ACCENTED_ASCII,
    SYNTHETIC_PERSON_AMBIGUOUS,
    SYNTHETIC_PERSON_AMBIGUOUS_ALT,
    SYNTHETIC_PERSON_DICOM,
    SYNTHETIC_PERSON_DICOM_ASCII,
    SYNTHETIC_PERSON_UNRELATED,
    SYNTHETIC_STUDY_DATE,
)
from workflows.imaging_workflow import ImagingWorkflow


def sorrimagem_message(
    *,
    message_id: str = "sorrimagem-message-001",
    archive_name: str = SYNTHETIC_ARCHIVE_NAME,
    transfer_url: str = "https://transfernow.net/dl/test-token",
) -> EmailMessage:
    return EmailMessage(
        message_id=message_id,
        subject=f'Sorrimagem enviou "{archive_name}"',
        sender="TransferNow <no-reply@transfernow.net>",
        reply_to="Sorrimagem <fixture1@example.com>",
        recipients=["fixture2@example.com"],
        received_at=datetime(2026, 7, 13, 15, 30, tzinfo=timezone.utc),
        text_body=(
            "Sorrimagem compartilhou um exame. "
            "Contato: fixture1@example.com"
        ),
        html_body=(
            "<p>O exame está disponível.</p>"
            f'<a href="{transfer_url}">Baixar no TransferNow</a>'
        ),
    )


def workflow_with_patients(names: list[str]) -> ImagingWorkflow:
    repository = InMemoryPatientRepository(
        Patient(id=index, nome=name)
        for index, name in enumerate(names, start=1)
    )
    return ImagingWorkflow(
        patient_resolver=PatientResolver(repository)
    )


def test_sorrimagem_email_produces_ready_dry_run_plan() -> None:
    workflow = workflow_with_patients([SYNTHETIC_PERSON_ACCENTED])

    plan = workflow.run_dry_run(sorrimagem_message())

    assert plan.message_id == "sorrimagem-message-001"
    assert plan.transfer_url == "https://transfernow.net/dl/test-token"
    assert plan.archive_name == SYNTHETIC_ARCHIVE_NAME
    assert plan.patient_name_candidate == SYNTHETIC_PERSON_ACCENTED
    assert plan.sender_email == "fixture1@example.com"
    assert plan.proposed_destination == (
        f"Radiology Intake/{SYNTHETIC_PERSON_ACCENTED}/{SYNTHETIC_ARCHIVE_NAME}"
    )
    assert plan.requires_manual_review is False
    assert plan.review_reasons == []
    assert plan.status == "DRY_RUN_READY"


def test_patient_matcher_accepts_exact_name() -> None:
    result = PatientMatcher.match(SYNTHETIC_PERSON_ACCENTED, [SYNTHETIC_PERSON_ACCENTED])

    assert result.score == 1.0
    assert result.selected_name == SYNTHETIC_PERSON_ACCENTED
    assert result.requires_manual_review is False


def test_patient_matcher_normalizes_accents_and_case() -> None:
    result = PatientMatcher.match(
        SYNTHETIC_PERSON_ACCENTED,
        [SYNTHETIC_PERSON_ACCENTED_ASCII.lower()],
    )

    assert result.score == 1.0
    assert result.selected_name == SYNTHETIC_PERSON_ACCENTED_ASCII.lower()


def test_patient_matcher_normalizes_dicom_caret_separator() -> None:
    result = PatientMatcher.match(
        SYNTHETIC_PERSON_DICOM,
        [SYNTHETIC_PERSON_DICOM_ASCII.lower()],
    )

    assert result.score == 1.0
    assert result.selected_name == SYNTHETIC_PERSON_DICOM_ASCII.lower()


def test_similar_patients_require_manual_review() -> None:
    message = sorrimagem_message(
        archive_name=f"{SYNTHETIC_PERSON_AMBIGUOUS}_{SYNTHETIC_STUDY_DATE}.zip"
    )
    match_result = PatientMatcher.match(
        SYNTHETIC_PERSON_AMBIGUOUS,
        [SYNTHETIC_PERSON_AMBIGUOUS, SYNTHETIC_PERSON_AMBIGUOUS_ALT],
    )

    plan = workflow_with_patients(
        [SYNTHETIC_PERSON_AMBIGUOUS, SYNTHETIC_PERSON_AMBIGUOUS_ALT],
    ).run_dry_run(message)

    assert match_result.selected_name is None
    assert len(match_result.candidates) == 2
    assert plan.requires_manual_review is True
    assert plan.status == "DRY_RUN_REVIEW_REQUIRED"
    assert "Correspondência ambígua entre pacientes." in plan.review_reasons
    assert "/REVIEW_REQUIRED/" in plan.proposed_destination


def test_low_score_requires_manual_review() -> None:
    message = sorrimagem_message(
        archive_name=f"{SYNTHETIC_PERSON_ACCENTED}_{SYNTHETIC_STUDY_DATE}.zip"
    )

    plan = workflow_with_patients([SYNTHETIC_PERSON_UNRELATED]).run_dry_run(message)

    assert plan.requires_manual_review is True
    assert "Correspondência abaixo do limiar automático." in plan.review_reasons
    assert plan.status == "DRY_RUN_REVIEW_REQUIRED"


def test_absent_patient_requires_manual_review() -> None:
    plan = workflow_with_patients([]).run_dry_run(sorrimagem_message())

    assert plan.requires_manual_review is True
    assert "Nenhum paciente disponível para comparação." in plan.review_reasons
    assert plan.status == "DRY_RUN_REVIEW_REQUIRED"


def test_malicious_transfernow_lookalike_is_rejected() -> None:
    message = sorrimagem_message(
        transfer_url="https://eviltransfernow.net/dl/test-token"
    )

    with pytest.raises(ValueError, match="link HTTPS válido"):
        workflow_with_patients([SYNTHETIC_PERSON_ACCENTED]).run_dry_run(message)


def test_reprocessing_message_id_reuses_the_same_logical_plan() -> None:
    workflow = workflow_with_patients([SYNTHETIC_PERSON_ACCENTED])
    message = sorrimagem_message()

    first_plan = workflow.run_dry_run(message)
    second_plan = workflow.run_dry_run(message)

    assert second_plan is first_plan
    assert workflow.service.planned_message_count == 1


def test_dry_run_does_not_use_network_or_create_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def forbidden_operation(*args, **kwargs):
        raise AssertionError("Operação externa proibida no dry-run")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden_operation)
    monkeypatch.setattr(socket, "create_connection", forbidden_operation)
    monkeypatch.setattr(Path, "write_text", forbidden_operation)
    monkeypatch.setattr(Path, "write_bytes", forbidden_operation)
    monkeypatch.setattr(Path, "touch", forbidden_operation)

    before = tuple(tmp_path.iterdir())
    plan = workflow_with_patients([SYNTHETIC_PERSON_ACCENTED]).run_dry_run(
        sorrimagem_message()
    )
    after = tuple(tmp_path.iterdir())

    assert plan.status == "DRY_RUN_READY"
    assert after == before
