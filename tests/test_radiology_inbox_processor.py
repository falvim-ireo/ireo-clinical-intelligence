"""Testes offline do comando manual da caixa radiológica."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import main as cli
from models.email_message import EmailMessage
from models.patient import Patient
from radiology.inbox_processor import RadiologyInboxProcessor
from radiology.patient_matcher import MatchStatus, PatientMatchResult
from radiology.transfernow_download import DownloadResult
from storage.onedrive_radiology_organizer import OrganizerStatus
from storage.radiology_storage import RadiologyStorage
from workflows.radiology_workflow import WorkflowResult


TRANSFER_URL = "https://www.transfernow.net/dl/public-example"


def email(message_id: str) -> EmailMessage:
    return EmailMessage(
        message_id=message_id,
        subject='TransferNow - "PACIENTE TESTE_20260721.zip"',
        sender="TransferNow <noreply@transfernow.net>",
        reply_to=None,
        received_at=datetime(2026, 7, 21),
        html_body=f'<a href="{TRANSFER_URL}">Download</a>',
    )


class FakeGmail:
    def __init__(self, messages: list[EmailMessage]) -> None:
        self.messages = messages
        self.query = ""
        self.labels: list[tuple[str, str]] = []

    def list_messages(self, query=None, max_results=None):
        self.query = query
        return self.messages[:max_results]

    def apply_label(self, message_id: str, label_name: str) -> None:
        self.labels.append((message_id, label_name))


class FakeWorkflow:
    """Simula TransferNow e o pipeline sem rede."""

    def __init__(self, root: Path, outcomes: dict[str, str]) -> None:
        self.root = root
        self.outcomes = outcomes
        self.calls: list[str] = []

    def run(self, **kwargs) -> WorkflowResult:
        message_id = kwargs["message_id"]
        self.calls.append(message_id)
        outcome = self.outcomes.get(message_id, "success")
        if outcome == "exception":
            raise RuntimeError("external secret")
        if outcome == "download-failure":
            return WorkflowResult(False, 0.1, None, None, None, None, ["download"])
        archive = self.root / f"{message_id}.zip"
        archive.write_bytes(b"PK simulated zip")
        match = PatientMatchResult(
            status=MatchStatus.EXACT,
            score=1.0,
            candidates=(),
            selected_patient=Patient(1, "PACIENTE TESTE"),
            reasons=("fixture",),
        )
        return WorkflowResult(
            True,
            0.1,
            DownloadResult(archive, archive.stat().st_size, "digest"),
            None,
            SimpleNamespace(study_instance_uid=f"uid-{message_id}"),
            match,
            [],
        )


class FakeClinicorp:
    def __init__(self) -> None:
        self.names: list[str | None] = []

    def __call__(self, transfer):
        self.names.append(transfer.patient_name_candidate)
        return [Patient(1, "PACIENTE TESTE")]


class FakeOneDriveOrganizer:
    def __init__(self, outcomes: list[OrganizerStatus]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def organize(self, workflow):
        status = self.outcomes[self.calls]
        self.calls += 1
        return SimpleNamespace(status=status)


def processor(tmp_path: Path, gmail, workflow, organizer, clinicorp=None):
    return RadiologyInboxProcessor(
        gmail=gmail,
        workflow=workflow,
        storage=RadiologyStorage(tmp_path / "storage"),
        organizer=organizer,
        patient_provider=clinicorp or FakeClinicorp(),
        download_root=tmp_path / "downloads",
    )


def test_processes_all_results_and_applies_terminal_labels(tmp_path: Path) -> None:
    gmail = FakeGmail([email("m1"), email("m2"), email("m3")])
    workflow = FakeWorkflow(tmp_path, {})
    clinicorp = FakeClinicorp()
    onedrive = FakeOneDriveOrganizer(
        [OrganizerStatus.UPLOADED, OrganizerStatus.DUPLICATE, OrganizerStatus.REVIEW_REQUIRED]
    )

    summary = processor(tmp_path, gmail, workflow, onedrive, clinicorp).run()

    assert (summary.processed, summary.duplicates, summary.review_required) == (1, 1, 1)
    assert summary.failed == 0
    assert gmail.labels == [
        ("m1", "IREO/Radiologia/Processado"),
        ("m2", "IREO/Radiologia/Duplicado"),
        ("m3", "IREO/Radiologia/Revisar"),
    ]
    assert clinicorp.names == ["PACIENTE TESTE"] * 3
    assert "-label:\"IREO/Radiologia/Processado\"" in gmail.query
    assert "-label:\"IREO/Radiologia/Falha\"" in gmail.query


def test_failure_is_isolated_and_next_message_continues(tmp_path: Path) -> None:
    gmail = FakeGmail([email("bad"), email("good")])
    workflow = FakeWorkflow(tmp_path, {"bad": "exception"})
    onedrive = FakeOneDriveOrganizer([OrganizerStatus.UPLOADED])

    summary = processor(tmp_path, gmail, workflow, onedrive).run()

    assert workflow.calls == ["bad", "good"]
    assert summary.failed == 1
    assert summary.processed == 1
    assert gmail.labels == [
        ("bad", "IREO/Radiologia/Falha"),
        ("good", "IREO/Radiologia/Processado"),
    ]


def test_workflow_failure_does_not_call_onedrive(tmp_path: Path) -> None:
    gmail = FakeGmail([email("bad")])
    workflow = FakeWorkflow(tmp_path, {"bad": "download-failure"})
    onedrive = FakeOneDriveOrganizer([])

    summary = processor(tmp_path, gmail, workflow, onedrive).run()

    assert summary.failed == 1
    assert onedrive.calls == 0
    assert gmail.labels == [("bad", "IREO/Radiologia/Falha")]


def test_label_is_applied_only_after_onedrive_result(tmp_path: Path) -> None:
    events: list[str] = []

    class Gmail(FakeGmail):
        def apply_label(self, message_id, label_name):
            events.append("label")
            super().apply_label(message_id, label_name)

    class OneDrive(FakeOneDriveOrganizer):
        def organize(self, workflow):
            events.append("upload")
            return super().organize(workflow)

    gmail = Gmail([email("m1")])
    processor(
        tmp_path,
        gmail,
        FakeWorkflow(tmp_path, {}),
        OneDrive([OrganizerStatus.UPLOADED]),
    ).run()

    assert events == ["upload", "label"]


def test_cli_returns_zero_even_with_individual_failures(monkeypatch, capsys) -> None:
    runner = SimpleNamespace(
        run=lambda **_kwargs: SimpleNamespace(
            found=2, processed=1, duplicates=0, review_required=0, failed=1
        )
    )
    monkeypatch.setattr(cli, "build_radiology_inbox_processor", lambda: runner)

    code = cli.main(["process-radiology-inbox"])

    assert code == 0
    output = capsys.readouterr().out
    assert "processadas=1" in output
    assert "falhas=1" in output


def test_cli_returns_nonzero_on_global_failure(monkeypatch, capsys) -> None:
    def fail():
        raise RuntimeError("secret")

    monkeypatch.setattr(cli, "build_radiology_inbox_processor", fail)

    assert cli.main(["process-radiology-inbox"]) == 1
    assert "não foi iniciado" in capsys.readouterr().out
