"""Observabilidade estruturada sem exposição de dados sensíveis."""

from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import logging
import socket
from uuid import uuid4

import pytest
import requests

from models.email_message import EmailMessage
from models.patient import Patient
from observability.audit_logger import (
    AuditEvent,
    AuditEventType,
    AuditLogger,
    emit_safely,
    mask_archive_name,
    mask_message_id,
    mask_patient_id,
    parse_log_level,
    safe_hostname,
)
from radiology.gmail_dry_run import run_gmail_dry_run
from repositories.clinicorp_patient_repository import (
    ClinicorpPatientRepository,
)
from repositories.patient_repository import InMemoryPatientRepository
from services.patient_resolver import PatientResolver
from workflows.imaging_workflow import ImagingWorkflow


@pytest.fixture(autouse=True)
def block_all_network(monkeypatch) -> None:
    def forbidden_network(*args, **kwargs):
        raise AssertionError("A observabilidade não pode acessar a rede")

    monkeypatch.setattr(requests, "get", forbidden_network)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden_network)
    monkeypatch.setattr(socket, "create_connection", forbidden_network)


def recording_audit(level: str = "DEBUG") -> tuple[AuditLogger, StringIO]:
    stream = StringIO()
    logger = logging.Logger(f"test-audit-{uuid4().hex}")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return (
        AuditLogger(
            logger=logger,
            level=level,
            correlation_id="correlation-test-0001",
        ),
        stream,
    )


def read_events(stream: StringIO) -> list[dict]:
    return [
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if line.strip()
    ]


def sensitive_message() -> EmailMessage:
    return EmailMessage(
        message_id="gmail-real-looking-sensitive-id-123",
        subject='TransferNow - "CLÁUDIA EXEMPLO FICTÍCIA_20991231.zip"',
        sender="TransferNow <no-reply@transfernow.net>",
        reply_to="Unidade Fictícia <origem-ficticia@example.invalid>",
        recipients=["destino-ficticio@example.invalid"],
        received_at=datetime(2099, 12, 31, tzinfo=timezone.utc),
        text_body=(
            "CORPO ULTRASSECRETO telefone +00 00 00000-0000 "
            "paciente.ficticio@example.invalid"
        ),
        html_body=(
            '<a href="https://transfernow.net/dl/TOKEN-ULTRASSECRETO'
            '?api_user=USUARIO-ULTRASSECRETO">Baixar</a>'
        ),
    )


class FakeGmailConnector:
    def __init__(self, messages: list[EmailMessage]) -> None:
        self.messages = messages

    def list_messages(self) -> list[EmailMessage]:
        return self.messages


class FailingAuditLogger:
    def emit(self, *args, **kwargs) -> None:
        raise RuntimeError("logger indisponível com TOKEN-ULTRASSECRETO")


class ErrorClinicorpAPI:
    def buscar_paciente(self, name: str, somente_ativos: bool = True):
        raise RuntimeError(
            "token=TOKEN-ULTRASSECRETO api_user=USUARIO-ULTRASSECRETO"
        )


def workflow_with_audit(
    audit_logger,
    patients: list[Patient],
) -> ImagingWorkflow:
    resolver = PatientResolver(
        InMemoryPatientRepository(patients),
        audit_logger=audit_logger,
    )
    return ImagingWorkflow(
        patient_resolver=resolver,
        audit_logger=audit_logger,
    )


def test_match_emits_expected_events_and_only_sanitized_identifiers() -> None:
    audit, stream = recording_audit("INFO")
    patient = Patient(
        id=990000010,
        nome="CLÁUDIA EXEMPLO FICTÍCIA",
        telefone="+00 00 00000-0000",
        email="paciente.ficticio@example.invalid",
    )
    workflow = workflow_with_audit(audit, [patient])

    result = run_gmail_dry_run(
        connector=FakeGmailConnector([sensitive_message()]),
        workflow=workflow,
        output=lambda value: None,
        audit_logger=audit,
    )
    events = read_events(stream)
    serialized = stream.getvalue()

    assert result == 0
    assert [event["event_type"] for event in events] == [
        "RADIOLOGY_EMAIL_DETECTED",
        "TRANSFERNOW_MESSAGE_PARSED",
        "PATIENT_RESOLUTION_STARTED",
        "PATIENT_RESOLUTION_MATCHED",
        "RADIOLOGY_DRY_RUN_COMPLETED",
    ]
    assert all(
        event["correlation_id"] == "correlation-test-0001"
        for event in events
    )
    assert all(event["timestamp"].endswith("Z") for event in events)
    assert events[1]["metadata"]["archive_host"] == "transfernow.net"
    assert events[3]["masked_patient_id"].startswith("patient-")

    forbidden = [
        "TOKEN-ULTRASSECRETO",
        "USUARIO-ULTRASSECRETO",
        "CORPO ULTRASSECRETO",
        "https://transfernow.net/dl/",
        "+00 00 00000-0000",
        "paciente.ficticio@example.invalid",
        "CLÁUDIA EXEMPLO FICTÍCIA",
        "gmail-real-looking-sensitive-id-123",
    ]
    assert all(value not in serialized for value in forbidden)


def test_review_and_unavailability_emit_operational_events() -> None:
    audit, stream = recording_audit("INFO")
    repository = ClinicorpPatientRepository(
        ErrorClinicorpAPI(),
        audit_logger=audit,
    )
    workflow = ImagingWorkflow(
        patient_resolver=PatientResolver(repository, audit_logger=audit),
        audit_logger=audit,
    )

    plan = workflow.run_dry_run(sensitive_message())
    events = read_events(stream)
    event_types = [event["event_type"] for event in events]
    serialized = stream.getvalue()

    assert plan.requires_manual_review is True
    assert event_types == [
        "TRANSFERNOW_MESSAGE_PARSED",
        "PATIENT_RESOLUTION_STARTED",
        "PATIENT_SOURCE_UNAVAILABLE",
        "PATIENT_RESOLUTION_REVIEW_REQUIRED",
        "RADIOLOGY_DRY_RUN_COMPLETED",
    ]
    assert event_types.count("PATIENT_SOURCE_UNAVAILABLE") == 1
    assert "TOKEN-ULTRASSECRETO" not in serialized
    assert "USUARIO-ULTRASSECRETO" not in serialized


def test_empty_source_emits_review_required_event() -> None:
    audit, stream = recording_audit("INFO")
    workflow = workflow_with_audit(audit, [])

    plan = workflow.run_dry_run(sensitive_message())
    events = read_events(stream)

    assert plan.requires_manual_review is True
    review = next(
        event
        for event in events
        if event["event_type"] == "PATIENT_RESOLUTION_REVIEW_REQUIRED"
    )
    assert review["reason_code"] == "PATIENT_NOT_FOUND"
    assert review["requires_manual_review"] is True


def test_dry_run_failure_is_logged_without_exception_details() -> None:
    audit, stream = recording_audit("INFO")
    workflow = workflow_with_audit(audit, [])
    invalid_message = sensitive_message()
    invalid_message.html_body = "sem link de download"

    with pytest.raises(ValueError):
        workflow.run_dry_run(invalid_message)

    events = read_events(stream)
    assert events == [
        {
            "correlation_id": "correlation-test-0001",
            "event_type": "RADIOLOGY_DRY_RUN_FAILED",
            "masked_message_id": mask_message_id(invalid_message.message_id),
            "masked_patient_id": None,
            "metadata": {},
            "reason_code": "DRY_RUN_ERROR",
            "requires_manual_review": True,
            "status": "FAILED",
            "timestamp": events[0]["timestamp"],
        }
    ]


def test_failing_logger_never_interrupts_the_workflow() -> None:
    failing = FailingAuditLogger()
    workflow = workflow_with_audit(
        failing,
        [Patient(id=990000010, nome="CLÁUDIA EXEMPLO FICTÍCIA")],
    )

    plan = workflow.run_dry_run(sensitive_message())

    assert plan.status == "DRY_RUN_READY"
    assert plan.requires_manual_review is False


def test_masks_and_hostname_are_deterministic_and_non_revealing() -> None:
    assert mask_message_id("sensitive") == mask_message_id("sensitive")
    assert mask_message_id("sensitive") != mask_message_id("different")
    assert mask_patient_id(990000010).startswith("patient-")
    assert "990000010" not in mask_patient_id(990000010)
    assert mask_message_id("") is None
    assert mask_patient_id(None) is None
    assert mask_archive_name("PACIENTE FICTÍCIO.zip").endswith(".zip")
    assert "PACIENTE" not in mask_archive_name("PACIENTE FICTÍCIO.zip")
    assert mask_archive_name("arquivo.extensão-longa").startswith("archive-")
    assert mask_archive_name(None) is None
    assert safe_hostname(
        "https://user:secret@transfernow.net:443/dl/token?secret=yes"
    ) == "transfernow.net"
    assert safe_hostname("https://domínio.invalid/path") is None
    assert safe_hostname("http://[") is None


def test_event_model_requires_timezone_aware_utc_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone"):
        AuditEvent(
            event_type=AuditEventType.RADIOLOGY_EMAIL_DETECTED,
            timestamp=datetime(2099, 1, 1),
            correlation_id="correlation-test-0001",
            status="DETECTED",
        )

    with pytest.raises(ValueError, match="UTC"):
        AuditEvent(
            event_type=AuditEventType.RADIOLOGY_EMAIL_DETECTED,
            timestamp=datetime(
                2099,
                1,
                1,
                tzinfo=timezone(timedelta(hours=-3)),
            ),
            correlation_id="correlation-test-0001",
            status="DETECTED",
        )


def test_allowlist_discards_unknown_metadata_even_in_debug() -> None:
    audit, stream = recording_audit("DEBUG")

    event = audit.emit(
        AuditEventType.PATIENT_RESOLUTION_MATCHED,
        status="status com conteúdo proibido",
        reason_code="reason com conteúdo proibido",
        archive_name="PACIENTE REAL.zip",
        download_url="https://transfernow.net/dl/secret-token",
        requires_manual_review="não",
        metadata={
            "candidate_count": 1,
            "matched_by": "exact_name",
            "patient_source": "clinicorp",
            "message_count": 2,
            "patient_name": "PACIENTE REAL",
            "phone": "+55 11 99999-9999",
            "email": "real@example.com",
            "token": "secret-token",
        },
    )

    assert event is not None
    assert event.status == "UNKNOWN"
    assert event.reason_code is None
    assert event.requires_manual_review is None
    assert event.metadata["candidate_count"] == 1
    assert event.metadata["matched_by"] == "exact_name"
    assert event.metadata["patient_source"] == "clinicorp"
    assert event.metadata["message_count"] == 2
    assert set(event.metadata) == {
        "candidate_count",
        "matched_by",
        "patient_source",
        "message_count",
        "archive_host",
        "masked_archive_name",
    }
    assert "PACIENTE REAL" not in stream.getvalue()
    assert "+55 11 99999-9999" not in stream.getvalue()
    assert "real@example.com" not in stream.getvalue()
    assert "secret-token" not in stream.getvalue()


def test_levels_invalid_inputs_and_logger_failures_are_safe() -> None:
    assert parse_log_level("INFO") == logging.INFO
    assert parse_log_level("DEBUG") == logging.DEBUG
    assert parse_log_level("unexpected") == logging.WARNING

    audit, stream = recording_audit("WARNING")
    assert audit.emit(
        AuditEventType.RADIOLOGY_EMAIL_DETECTED,
        status="DETECTED",
    ) is not None
    assert stream.getvalue() == ""
    assert audit.emit("INVALID_EVENT", status="FAILED") is None

    class ExplodingLogger:
        def setLevel(self, level) -> None:
            self.level = level

        def log(self, *args, **kwargs) -> None:
            raise RuntimeError("logging failure")

    exploding = AuditLogger(logger=ExplodingLogger(), level="INFO")
    assert exploding.emit(
        AuditEventType.RADIOLOGY_EMAIL_DETECTED,
        status="DETECTED",
    ) is None
    emit_safely(None, AuditEventType.RADIOLOGY_EMAIL_DETECTED, status="OK")
    emit_safely(
        FailingAuditLogger(),
        AuditEventType.RADIOLOGY_EMAIL_DETECTED,
        status="OK",
    )


def test_default_logger_and_generated_correlation_id_are_available() -> None:
    audit = AuditLogger(level="WARNING", correlation_id="inválido")

    assert len(audit.correlation_id) == 32
    assert audit._logger.name.startswith("ireo.audit.")
    assert audit._logger.handlers
