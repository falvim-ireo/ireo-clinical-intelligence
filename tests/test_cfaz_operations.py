"""Cobertura da interface operacional Cfaz da Sprint 15.1."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import main
import pytest

from acquisition.cfaz_operations import (
    CfazHistoryRepository, CfazNotificationCatalog,
)
from acquisition.service import ProviderAcquisitionService
from acquisition.cfaz_provider import CfazAmbiguousRequestError
from core.config import Config
from models.email_message import EmailMessage
from radiology.exam_index_service import ExamIndexService
from synthetic_fixtures import (
    SYNTHETIC_PATIENT_ALPHA,
    SYNTHETIC_PATIENT_BETA,
    SYNTHETIC_PATIENT_GAMMA,
    SYNTHETIC_REQUEST_ID,
    synthetic_patient_name,
    synthetic_request_id,
)


def message(request_id: str, patient: str, *, hour: int = 9) -> EmailMessage:
    return EmailMessage(
        message_id=f"internal-{request_id}",
        subject=f"CfazPost - {patient}",
        sender="SORRIMAGEM <noreply@cfaz.net>", reply_to=None,
        received_at=datetime(2026, 7, 20, hour, 31, tzinfo=timezone.utc),
        text_body=(
            f"PACIENTE SINTÉTICO 07: {patient}\n"
            f"https://max.cfaz.net/requests/{request_id}"
        ),
    )


class Gmail:
    def __init__(self, values): self.values = values; self.calls = []
    def authenticated_account(self): return "fixture1@example.com"
    def list_provider_notifications(self, **kwargs):
        self.calls.append(kwargs)
        return self.values


def test_catalog_extracts_request_patient_date_deduplicates_and_marks_imported(tmp_path):
    history = CfazHistoryRepository(tmp_path / "index.db")
    history.mark_started(
        request_id=synthetic_request_id(1), message_id="secret", patient_name=synthetic_patient_name(6)
    )
    history.mark_complete(
        request_id=synthetic_request_id(1), patient_name=synthetic_patient_name(6), duration_seconds=12.5,
        onedrive_destination="PACIENTE SINTÉTICO 07s/PACIENTE SINTÉTICO 10/Radiologia/2026-07-20 - Panorâmica",
    )
    gmail = Gmail([
        message(synthetic_request_id(2), synthetic_patient_name(5)),
        message(synthetic_request_id(1), synthetic_patient_name(6), hour=10),
        message(synthetic_request_id(2), synthetic_patient_name(5), hour=11),
    ])
    catalog = CfazNotificationCatalog(
        gmail=gmail, history=history, query="cfaz-query", limit=100
    )

    values = catalog.list()

    assert [item.request_id for item in values] == [synthetic_request_id(2), synthetic_request_id(1)]
    assert [item.patient_name for item in values] == [synthetic_patient_name(5), synthetic_patient_name(6)]
    assert [item.imported for item in values] == [False, True]
    assert [item.request_id for item in catalog.pending(values)] == [synthetic_request_id(2)]
    assert gmail.calls == [{"query": "cfaz-query", "max_results": 100}]


def test_catalog_accepts_real_cfazpost_html_and_encoded_redirect(tmp_path):
    real = EmailMessage(
        message_id="must-not-be-shown",
        sender="SORRIMAGEM <noreply@cfaz.net>", reply_to=None,
        subject="CfazPost - PACIENTE SINTÉTICO 01",
        received_at=datetime(2026, 7, 20, 9, 31, tzinfo=timezone.utc),
        html_body=(
            '<a href="https://tracker.example/redirect?target='
            f'https%3A%2F%2Fmax.cfaz.net%2Frequests%2F{synthetic_request_id(2)}%3Fsource%3Demail">'
            "Abrir pedido</a>"
        ),
    )
    catalog = CfazNotificationCatalog(
        gmail=Gmail([real]), history=CfazHistoryRepository(tmp_path / "index.db"),
        query="from:cfaz.net newer_than:365d", limit=100,
    )

    values = catalog.list()

    assert len(values) == 1
    assert values[0].request_id == synthetic_request_id(2)
    assert values[0].patient_name == synthetic_patient_name(1)
    assert values[0].accepted is True
    assert catalog.counters.gmail_messages == 1
    assert catalog.counters.cfazpost_subjects == 1
    assert catalog.counters.recognized_links == 1
    assert catalog.counters.operational_notifications == 1


def test_recognized_notification_without_request_is_visible_but_not_pending(tmp_path):
    value = EmailMessage(
        message_id="hidden", sender="SORRIMAGEM <noreply@cfaz.net>",
        reply_to=None, subject="cfazpost - PACIENTE SINTÉTICO 03",
        received_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
        html_body="<p>Seu pedido está disponível.</p>",
    )
    catalog = CfazNotificationCatalog(
        gmail=Gmail([value]), history=CfazHistoryRepository(tmp_path / "index.db"),
        query="query", limit=100,
    )

    listed = catalog.list()

    assert len(listed) == 1
    assert listed[0].request_id is None
    assert listed[0].accepted is False
    assert listed[0].rejection_reason == "link de pedido não encontrado"
    assert catalog.pending(listed) == []


def test_history_persists_success_failure_duration_and_destination(tmp_path):
    database = tmp_path / "index.db"
    history = CfazHistoryRepository(database)
    history.mark_started(request_id="1", message_id="gmail-1", patient_name=synthetic_patient_name(12))
    history.mark_complete(
        request_id="1", patient_name=synthetic_patient_name(12), duration_seconds=5.25,
        onedrive_destination="PACIENTE SINTÉTICO 07s/PACIENTE SINTÉTICO 12/Radiologia/Exame",
    )
    history.mark_started(request_id="2", message_id="gmail-2", patient_name=synthetic_patient_name(11))
    history.mark_failed(
        request_id="2", patient_name=synthetic_patient_name(11), duration_seconds=2.0,
        error_code="TimeoutError",
    )

    reopened = CfazHistoryRepository(database)
    records = {record.request_id: record for record in reopened.list_records()}
    assert records["1"].status == "COMPLETE"
    assert records["1"].duration_seconds == 5.25
    assert records["1"].onedrive_destination.endswith("/Exame")
    assert records["2"].status == "FAILED"
    assert reopened.is_imported("1") is True
    assert reopened.is_imported("2") is False


def test_manual_archive_is_distinct_from_empty_cfaz_acquisition_history(tmp_path):
    database = tmp_path / "index.db"
    ExamIndexService(database).index_manifest({
        "onedrive_destination": "PACIENTE SINTÉTICO 07s/Legado/Radiologia/2020-01-01 - Radiologia",
        "publication": {
            "exam_id": "a" * 64,
            "source_archive_sha256": "b" * 64,
            "state": "COMPLETE",
        },
        "dicom_intelligence": {"studies": [], "alerts": []},
    }, patient_name=synthetic_patient_name(2), source="rebuild")
    history = CfazHistoryRepository(database)

    # Acervo, timeline e dashboard existentes não constituem aquisição Cfaz.
    assert history.is_imported(synthetic_request_id(2)) is False
    with history._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM exams").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM cfaz_import_history").fetchone()[0] == 0

    # IN_PROGRESS ainda pode ser retomado e somente COMPLETE torna o pedido importado.
    history.mark_started(
        request_id=synthetic_request_id(2), message_id="internal", patient_name=synthetic_patient_name(7),
        provider_exam_id="exam-provider-1",
        provider_request_id=synthetic_request_id(3), sequential_id=synthetic_request_id(4),
        clinic_number=synthetic_request_id(5),
    )
    assert history.is_imported(synthetic_request_id(2)) is False
    history.mark_complete(
        request_id=synthetic_request_id(2), patient_name=synthetic_patient_name(7), duration_seconds=5,
        onedrive_destination="PACIENTE SINTÉTICO 07s/PACIENTE SINTÉTICO 07/Radiologia/Exame",
        provider_exam_id="exam-provider-1", acquisition_sha="c" * 64,
        provider_request_id=synthetic_request_id(3), sequential_id=synthetic_request_id(4),
        clinic_number=synthetic_request_id(5),
    )
    assert CfazHistoryRepository(database).is_imported(synthetic_request_id(2)) is True
    record = history.list_records()[0]
    assert record.provider_exam_id == "exam-provider-1"
    assert record.acquisition_sha == "c" * 64
    assert record.import_timestamp
    assert record.provider_request_id == synthetic_request_id(3)
    assert record.sequential_id == synthetic_request_id(4)
    assert record.clinic_number == synthetic_request_id(5)
    assert history.is_imported(synthetic_request_id(3)) is True
    assert history.is_imported(synthetic_request_id(4)) is True
    second_run = CfazNotificationCatalog(
        gmail=Gmail([message(synthetic_request_id(2), synthetic_patient_name(7))]), history=history,
        query="query", limit=100,
    )
    listed = second_run.list()
    assert listed[0].imported is True
    assert second_run.pending(listed) == []


def test_provider_service_records_complete_operational_history(tmp_path):
    request = SimpleNamespace(
        request_id=synthetic_request_id(2), patient_name=synthetic_patient_name(9), provider_id="cfaz",
        provider_exam_id=None, exam_date=None,
        manifest_metadata=lambda: {"provider_id": "cfaz", "request_id": synthetic_request_id(2)},
    )
    package = SimpleNamespace(
        request=request, archive_path=tmp_path / "package.zip", sha256="a" * 64,
    )
    package.archive_path.write_bytes(b"zip")

    class Provider:
        def authenticate(self): pass
        def discover(self, notification): return (request,)
        def download(self, *args): return package
        def finalize(self, *args, **kwargs): pass

    class Importer:
        def run(self, **kwargs):
            return SimpleNamespace(onedrive_destination="PACIENTE SINTÉTICO 07s/PACIENTE SINTÉTICO 09/Radiologia/Exame")

    clock = iter((10.0, 14.5))
    history = CfazHistoryRepository(tmp_path / "history.db")
    ProviderAcquisitionService(
        provider=Provider(), importer=Importer(), quarantine_root=tmp_path,
        correlation_id="correlation", history=history,
        monotonic_provider=lambda: next(clock), output=lambda _: None,
    ).run(message(synthetic_request_id(2), synthetic_patient_name(9)))

    record = history.list_records()[0]
    assert record.status == "COMPLETE"
    assert record.duration_seconds == 4.5
    assert record.onedrive_destination.endswith("/Exame")


def test_ambiguous_visible_number_is_recorded_without_import(tmp_path):
    class Provider:
        def authenticate(self): pass
        def discover_request(self, value):
            raise CfazAmbiguousRequestError("ambíguo")

    history = CfazHistoryRepository(tmp_path / "history.db")
    service = ProviderAcquisitionService(
        provider=Provider(), importer=object(), quarantine_root=tmp_path,
        correlation_id="correlation", history=history, output=lambda _: None,
    )

    with pytest.raises(CfazAmbiguousRequestError):
        service.run_request_id(synthetic_request_id(4))

    record = history.list_records()[0]
    assert record.status == "AMBIGUOUS"
    assert record.sequential_id == synthetic_request_id(4)
    assert record.provider_request_id is None


def test_list_and_history_commands_hide_message_id(tmp_path, monkeypatch, capsys):
    values = [message(synthetic_request_id(2), synthetic_patient_name(5))]
    database = tmp_path / "index.db"
    monkeypatch.setattr(Config, "IREO_RADIOLOGY_INDEX_DATABASE_PATH", str(database))
    monkeypatch.setattr(Config, "CFAZ_GMAIL_QUERY", "query")
    monkeypatch.setattr(Config, "CFAZ_GMAIL_MAX_MESSAGES", 100)
    import integrations.gmail_connector as gmail_module
    monkeypatch.setattr(gmail_module, "GmailConnector", lambda: Gmail(values))

    assert main.main(["cfaz-list-notifications"]) == 0
    output = capsys.readouterr().out
    assert f"Paciente: {synthetic_patient_name(5)}" in output
    assert "Pedido: 99000002" in output
    assert "Status: NÃO IMPORTADO" in output
    assert "internal-99000002" not in output

    history = CfazHistoryRepository(database)
    history.mark_started(request_id=synthetic_request_id(2), message_id="internal-99000002", patient_name=synthetic_patient_name(9))
    history.mark_complete(
        request_id=synthetic_request_id(2), patient_name=synthetic_patient_name(9), duration_seconds=3.0,
        onedrive_destination="PACIENTE SINTÉTICO 07s/PACIENTE SINTÉTICO 09/Radiologia/Exame",
    )
    assert main.main(["cfaz-history"]) == 0
    output = capsys.readouterr().out
    assert "pedido=99000002" in output and "status=COMPLETE" in output
    assert "internal-99000002" not in output


def test_list_debug_reports_safe_diagnostics_and_effective_query(
    tmp_path, monkeypatch, capsys,
):
    valid = message(synthetic_request_id(2), synthetic_patient_name(1))
    missing = EmailMessage(
        message_id="raw-secret-id", sender="SORRIMAGEM <noreply@cfaz.net>",
        reply_to=None, subject="CfazPost - PACIENTE SINTÉTICO 03",
        received_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
        html_body="<p>sem link</p>",
    )
    monkeypatch.setattr(Config, "IREO_RADIOLOGY_INDEX_DATABASE_PATH",
                        str(tmp_path / "index.db"))
    monkeypatch.setattr(Config, "CFAZ_GMAIL_QUERY",
                        "from:cfaz.net newer_than:365d")
    import integrations.gmail_connector as gmail_module
    monkeypatch.setattr(gmail_module, "GmailConnector", lambda: Gmail([valid, missing]))

    assert main.main(["cfaz-list-notifications", "--debug"]) == 0

    output = capsys.readouterr().out
    assert "Conta Gmail autenticada: fixture1@example.com" in output
    assert "Consulta Gmail: from:cfaz.net newer_than:365d" in output
    assert "Mensagens retornadas pelo Gmail: 2" in output
    assert "Mensagens com assunto CfazPost: 2" in output
    assert "Mensagens com link reconhecido: 1" in output
    assert "Notificações operacionais: 1" in output
    assert "Request ID encontrado: não" in output
    assert "Motivo da rejeição: link de pedido não encontrado" in output
    assert "Status: notificação reconhecida, pedido não identificado" in output
    assert "raw-secret-id" not in output
    assert "https://" not in output

def _patch_import_runtime(monkeypatch, values, selected_ids):
    import acquisition.cfaz_provider as provider_module
    import acquisition.service as service_module
    import integrations.gmail_connector as gmail_module
    import radiology.exam_index_service as index_module
    import radiology.intake_history as intake_module
    import radiology.supervised_import as importer_module
    import repositories.clinicorp_patient_repository as patient_module

    monkeypatch.setattr(gmail_module, "GmailConnector", lambda: Gmail(values))
    monkeypatch.setattr(provider_module, "CfazProvider", lambda **kwargs: object())
    monkeypatch.setattr(importer_module, "SupervisedRadiologyImporter",
                        lambda **kwargs: object())
    monkeypatch.setattr(index_module, "ExamIndexService", lambda *args: object())
    monkeypatch.setattr(intake_module, "IntakeHistoryRepository",
                        lambda *args: object())
    monkeypatch.setattr(patient_module, "ClinicorpPatientRepository",
                        lambda *args, **kwargs: object())
    monkeypatch.setattr(main, "build_onedrive_graph_client", lambda: object())
    monkeypatch.setattr(main, "ClinicorpAPI", lambda: object())

    class Service:
        def __init__(self, **kwargs): pass
        def run(self, notification):
            request_id = notification.text_body.rsplit("/", 1)[-1]
            selected_ids.append(request_id)
            return (SimpleNamespace(onedrive_destination="OneDrive/exame"),)
        def run_request_id(self, request_id):
            selected_ids.append(request_id)
            return (SimpleNamespace(onedrive_destination="OneDrive/exame"),)

    monkeypatch.setattr(service_module, "ProviderAcquisitionService", Service)


def test_import_by_request_id_does_not_require_gmail_message_id(
    tmp_path, monkeypatch, capsys,
):
    selected = []
    monkeypatch.setattr(Config, "IREO_RADIOLOGY_INDEX_DATABASE_PATH",
                        str(tmp_path / "index.db"))
    _patch_import_runtime(
        monkeypatch,
        [message(synthetic_request_id(2), synthetic_patient_name(9)), message(synthetic_request_id(1), synthetic_patient_name(10))], selected,
    )
    import integrations.gmail_connector as gmail_module
    import acquisition.cfaz_provider as provider_module
    provider_options = {}
    monkeypatch.setattr(
        provider_module, "CfazProvider",
        lambda **kwargs: provider_options.update(kwargs) or object(),
    )
    monkeypatch.setattr(
        gmail_module, "GmailConnector",
        lambda: (_ for _ in ()).throw(AssertionError("Gmail não deve ser acessado")),
    )

    assert main.main([
        "radiology-import-from-cfaz", "--request-id", synthetic_request_id(1), "--debug-auth",
        "--debug-payload",
    ]) == 0

    assert selected == [synthetic_request_id(1)]
    assert provider_options["auth_diagnostics"] is True
    assert provider_options["payload_diagnostics"] is True
    assert "internal-99000001" not in capsys.readouterr().out


def test_explicit_completed_request_stops_before_gmail_or_provider(
    tmp_path, monkeypatch, capsys,
):
    database = tmp_path / "index.db"
    monkeypatch.setattr(Config, "IREO_RADIOLOGY_INDEX_DATABASE_PATH", str(database))
    history = CfazHistoryRepository(database)
    history.mark_complete(
        request_id=synthetic_request_id(3), patient_name=synthetic_patient_name(7), duration_seconds=1,
        onedrive_destination="OneDrive/exame", provider_exam_id="exam-1",
        acquisition_sha="a" * 64,
        provider_request_id=synthetic_request_id(3), sequential_id=synthetic_request_id(4),
        clinic_number=synthetic_request_id(5),
    )
    import integrations.gmail_connector as gmail_module
    import acquisition.cfaz_provider as provider_module
    monkeypatch.setattr(
        gmail_module, "GmailConnector",
        lambda: (_ for _ in ()).throw(AssertionError("Gmail não deve ser acessado")),
    )
    monkeypatch.setattr(
        provider_module, "CfazProvider",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("API não deve ser acessada")),
    )

    assert main.main([
        "radiology-import-from-cfaz", "--request-id", synthetic_request_id(4)
    ]) == 0

    assert "já foi importado" in capsys.readouterr().out


def test_select_imports_chosen_pending_notification(tmp_path, monkeypatch):
    selected = []
    database = tmp_path / "index.db"
    monkeypatch.setattr(Config, "IREO_RADIOLOGY_INDEX_DATABASE_PATH", str(database))
    history = CfazHistoryRepository(database)
    history.mark_started(request_id=synthetic_request_id(2), message_id="hidden", patient_name=synthetic_patient_name(9))
    history.mark_complete(
        request_id=synthetic_request_id(2), patient_name=synthetic_patient_name(9), duration_seconds=1,
        onedrive_destination="OneDrive/PACIENTE SINTÉTICO 09",
    )
    _patch_import_runtime(
        monkeypatch,
        [message(synthetic_request_id(2), synthetic_patient_name(9)), message(synthetic_request_id(1), synthetic_patient_name(10)),
         message(synthetic_request_id(6), synthetic_patient_name(8))], selected,
    )
    monkeypatch.setattr("builtins.input", lambda _: "2")

    assert main.main(["radiology-import-from-cfaz", "--select"]) == 0

    assert selected == [synthetic_request_id(6)]


def test_default_imports_all_and_only_pending_notifications(tmp_path, monkeypatch):
    selected = []
    database = tmp_path / "index.db"
    monkeypatch.setattr(Config, "IREO_RADIOLOGY_INDEX_DATABASE_PATH", str(database))
    history = CfazHistoryRepository(database)
    history.mark_started(request_id=synthetic_request_id(1), message_id="hidden", patient_name=synthetic_patient_name(10))
    history.mark_complete(
        request_id=synthetic_request_id(1), patient_name=synthetic_patient_name(10), duration_seconds=1,
        onedrive_destination="OneDrive/PACIENTE SINTÉTICO 10",
    )
    _patch_import_runtime(
        monkeypatch,
        [message(synthetic_request_id(2), synthetic_patient_name(9)), message(synthetic_request_id(1), synthetic_patient_name(10)),
         message(synthetic_request_id(6), synthetic_patient_name(8))], selected,
    )

    assert main.main(["radiology-import-from-cfaz"]) == 0

    assert selected == [synthetic_request_id(2), synthetic_request_id(6)]


def test_unidentified_notifications_are_not_counted_as_imported(
    tmp_path, monkeypatch, capsys,
):
    unidentified = EmailMessage(
        message_id="hidden", sender="SORRIMAGEM <noreply@cfaz.net>",
        reply_to=None, subject="CfazPost - PACIENTE SINTÉTICO 04",
        received_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
        html_body="<p>sem link reconhecível</p>",
    )
    monkeypatch.setattr(Config, "IREO_RADIOLOGY_INDEX_DATABASE_PATH",
                        str(tmp_path / "index.db"))
    import integrations.gmail_connector as gmail_module
    monkeypatch.setattr(gmail_module, "GmailConnector", lambda: Gmail([unidentified]))

    assert main.main(["radiology-import-from-cfaz"]) == 0

    output = capsys.readouterr().out
    assert "0 já importadas" in output
    assert "1 com pedido não identificado" in output
    assert "0 novas" in output
