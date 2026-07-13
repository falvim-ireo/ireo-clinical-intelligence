import base64
from datetime import timezone
from pathlib import Path

import pytest
import requests

from core.config import Config
from integrations.gmail_connector import (
    GmailConnector,
    GmailCredentialsError,
)
from models.patient import Patient
from radiology.gmail_dry_run import run_gmail_dry_run
from repositories.patient_repository import InMemoryPatientRepository
from services.patient_resolver import PatientResolver
from workflows.imaging_workflow import ImagingWorkflow


def encode_body(content: str) -> str:
    return base64.urlsafe_b64encode(content.encode("utf-8")).decode("ascii")


def gmail_message(
    *,
    message_id: str = "gmail-message-id-001",
    subject: str = 'TransferNow - "JOÃO SILVA_20260713.zip"',
    reply_to: str = "Sorrimagem <atendimento@sorrimagem.example>",
    text_body: str | None = None,
    html_body: str | None = None,
) -> dict:
    parts = []
    if text_body is not None:
        parts.append(
            {
                "mimeType": "text/plain",
                "body": {"data": encode_body(text_body)},
            }
        )
    if html_body is not None:
        parts.append(
            {
                "mimeType": "text/html",
                "body": {"data": encode_body(html_body)},
            }
        )

    return {
        "id": message_id,
        "internalDate": "1783956600000",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": subject},
                {
                    "name": "From",
                    "value": "TransferNow <noreply@transfernow.net>",
                },
                {"name": "Reply-To", "value": reply_to},
                {
                    "name": "To",
                    "value": "Radiologia <radiologia@ireo.example>",
                },
                {
                    "name": "Date",
                    "value": "Mon, 13 Jul 2026 15:30:00 -0300",
                },
            ],
            "parts": parts,
        },
    }


class FakeRequest:
    def __init__(self, response: dict) -> None:
        self.response = response

    def execute(self) -> dict:
        return self.response


class ReadOnlyFakeMessages:
    def __init__(self, raw_messages: list[dict]) -> None:
        self.raw_messages = {
            message["id"]: message
            for message in raw_messages
        }
        self.operations = []

    def list(self, **kwargs) -> FakeRequest:
        self.operations.append(("list", kwargs))
        return FakeRequest(
            {
                "messages": [
                    {"id": message_id}
                    for message_id in self.raw_messages
                ]
            }
        )

    def get(self, **kwargs) -> FakeRequest:
        self.operations.append(("get", kwargs))
        return FakeRequest(self.raw_messages[kwargs["id"]])


class ReadOnlyFakeUsers:
    def __init__(self, messages: ReadOnlyFakeMessages) -> None:
        self._messages = messages

    def messages(self) -> ReadOnlyFakeMessages:
        return self._messages


class ReadOnlyFakeGmailService:
    def __init__(self, raw_messages: list[dict]) -> None:
        self.messages_api = ReadOnlyFakeMessages(raw_messages)
        self._users = ReadOnlyFakeUsers(self.messages_api)

    def users(self) -> ReadOnlyFakeUsers:
        return self._users


def workflow_with_patients(names: list[str]) -> ImagingWorkflow:
    repository = InMemoryPatientRepository(
        Patient(id=index, nome=name)
        for index, name in enumerate(names, start=1)
    )
    return ImagingWorkflow(
        patient_resolver=PatientResolver(repository)
    )


def test_parses_multipart_plain_text_and_html() -> None:
    raw = gmail_message(
        text_body="Mensagem em texto simples.",
        html_body=(
            '<a href="https://transfernow.net/dl/example">Baixar</a>'
        ),
    )

    message = GmailConnector.parse_message(raw)

    assert message.message_id == "gmail-message-id-001"
    assert message.text_body == "Mensagem em texto simples."
    assert "https://transfernow.net/dl/example" in message.html_body
    assert message.sender == "TransferNow <noreply@transfernow.net>"
    assert message.recipients == ["radiologia@ireo.example"]
    assert message.received_at.utcoffset().total_seconds() == -10800


def test_parses_html_message_without_plain_text() -> None:
    raw = gmail_message(
        html_body=(
            '<a href="https://transfernow.net/dl/html-only">Baixar</a>'
        )
    )

    message = GmailConnector.parse_message(raw)

    assert message.text_body == ""
    assert "html-only" in message.html_body


def test_preserves_truncated_subject_and_sorrimagem_reply_to() -> None:
    raw = gmail_message(
        subject="TransferNow - envio de exame...",
        reply_to="Sorrimagem <contato@sorrimagem.example>",
        html_body="<p>Mensagem sem arquivo completo.</p>",
    )

    message = GmailConnector.parse_message(raw)

    assert message.subject == "TransferNow - envio de exame..."
    assert message.reply_to == "Sorrimagem <contato@sorrimagem.example>"


def test_message_without_link_is_rejected_by_dry_run_workflow() -> None:
    message = GmailConnector.parse_message(
        gmail_message(text_body="TransferNow sem link.")
    )

    with pytest.raises(ValueError, match="link HTTPS válido"):
        workflow_with_patients(["JOÃO SILVA"]).run_dry_run(message)


def test_malicious_link_is_rejected_by_dry_run_workflow() -> None:
    message = GmailConnector.parse_message(
        gmail_message(
            html_body=(
                '<a href="https://eviltransfernow.net/dl/example">Baixar</a>'
            )
        )
    )

    with pytest.raises(ValueError, match="link HTTPS válido"):
        workflow_with_patients(["JOÃO SILVA"]).run_dry_run(message)


def test_missing_credentials_fails_before_network(tmp_path: Path) -> None:
    connector = GmailConnector(
        credentials_file=tmp_path / "missing-credentials.json",
        token_file=tmp_path / "missing-token.json",
    )

    with pytest.raises(GmailCredentialsError, match="não foram encontradas"):
        connector.list_messages()


def test_connector_uses_only_read_operations_and_safe_pilot_limit() -> None:
    raw = gmail_message(
        text_body="Contato: atendimento@sorrimagem.example",
        html_body=(
            '<a href="https://transfernow.net/dl/example">Baixar</a>'
        ),
    )
    service = ReadOnlyFakeGmailService([raw])
    connector = GmailConnector(service=service)

    messages = connector.list_messages(max_results=100)

    assert len(messages) == 1
    assert [operation[0] for operation in service.messages_api.operations] == [
        "list",
        "get",
    ]
    list_arguments = service.messages_api.operations[0][1]
    assert list_arguments == {
        "userId": "me",
        "q": "from:noreply@transfernow.net subject:TransferNow",
        "maxResults": 5,
    }
    get_arguments = service.messages_api.operations[1][1]
    assert get_arguments["format"] == "full"
    assert GmailConnector.SCOPES == (
        "https://www.googleapis.com/auth/gmail.readonly",
    )


def test_gmail_message_id_preserves_workflow_idempotency() -> None:
    raw = gmail_message(
        text_body="Contato: atendimento@sorrimagem.example",
        html_body=(
            '<a href="https://transfernow.net/dl/example">Baixar</a>'
        ),
    )
    message = GmailConnector.parse_message(raw)
    workflow = workflow_with_patients(["JOÃO SILVA"])

    first = workflow.run_dry_run(message)
    second = workflow.run_dry_run(message)

    assert second is first
    assert workflow.service.planned_message_count == 1


def test_runner_has_no_transfernow_or_onedrive_network_side_effects(
    monkeypatch,
    capsys,
) -> None:
    def forbidden_network(*args, **kwargs):
        raise AssertionError("Chamadas de rede adicionais são proibidas")

    raw = gmail_message(
        message_id="1783959118015abcdef",
        text_body=(
            "Corpo confidencial. Contato: atendimento@sorrimagem.example"
        ),
        html_body=(
            '<a href="https://transfernow.net/dl/secret-token">Baixar</a>'
        ),
    )
    service = ReadOnlyFakeGmailService([raw])
    connector = GmailConnector(service=service)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden_network)

    result = run_gmail_dry_run(
        connector=connector,
        workflow=workflow_with_patients(["JOÃO SILVA"]),
    )
    output = capsys.readouterr().out

    assert result == 0
    assert "1783...cdef" in output
    assert "1783959118015abcdef" not in output
    assert "secret-token" not in output
    assert "Corpo confidencial" not in output
    assert "JOÃO SILVA_20260713.zip" in output
    assert "JOÃO SILVA" in output


def test_cli_dispatches_gmail_dry_run_without_changing_default_flow(
    monkeypatch,
) -> None:
    import main
    from radiology import gmail_dry_run

    calls = []
    monkeypatch.setattr(
        gmail_dry_run,
        "run_gmail_dry_run",
        lambda: calls.append("gmail") or 0,
    )

    assert main.main(["radiology-gmail-dry-run"]) == 0
    assert calls == ["gmail"]


def test_gmail_config_uses_safe_defaults() -> None:
    assert Config.GMAIL_QUERY == (
        "from:noreply@transfernow.net subject:TransferNow"
    )
    assert 1 <= Config.GMAIL_MAX_MESSAGES <= 5
