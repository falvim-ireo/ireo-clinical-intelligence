import json
from datetime import datetime, timezone
from types import SimpleNamespace

from radiology.auto_run import RadiologyAutoRunner, sanitized_history_failure
from radiology.intake_history import IntakeHistoryError, IntakeHistoryRepository
from radiology.transfernow_download import DownloadResult
from models.patient import Patient


class NoGmail:
    def list_messages(self, **kwargs):
        raise AssertionError("Gmail não deve ser consultado desligado")


class Importer:
    class Audit:
        correlation_id = "fixture-correlation"
    audit_logger = Audit()
    quarantine_root = "quarantine"


class Messages:
    def __init__(self, messages): self.messages = messages
    def list_messages(self, **kwargs): return self.messages


class BrokenGmail:
    def list_messages(self, **kwargs): raise RuntimeError("network token patient path")


class BrowserRequired:
    def download(self, *args):
        from radiology.transfernow_download import BrowserInteractionRequired
        raise BrowserInteractionRequired("review")


def auto_message():
    return SimpleNamespace(
        message_id="m-visible", text_body="x", html_body="", subject="x",
        received_at=datetime.now(timezone.utc),
    )


def configure_transfer(monkeypatch):
    from radiology import auto_run
    monkeypatch.setattr(
        auto_run.TransferNowConnector,
        "interpretar",
        lambda *args: SimpleNamespace(
            original_filename="safe.zip", patient_name_candidate="SAFE",
            download_url="https://transfernow.net/dl/public-token",
        ),
    )


class FailedBrowser:
    headless = None
    allow_manual_interaction = True

    def download(self, *args):
        raise RuntimeError("browser automation stopped")


def test_auto_run_disabled_does_not_call_gmail_and_writes_sanitized_summary(tmp_path):
    output = tmp_path / "summary.json"
    runner = RadiologyAutoRunner(
        gmail=NoGmail(), downloader=None, importer=Importer(),
        history=IntakeHistoryRepository(tmp_path / "intake.db"), enabled=False,
        allow_copy=False, max_messages=3, summary_path=output,
    )
    code, summary = runner.run()
    assert code == 0
    assert summary.messages_found == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["completed"] == 0
    assert "fixture-correlation" not in output.read_text(encoding="utf-8")


def test_three_review_messages_have_unique_correlations_and_shared_run_id(tmp_path, monkeypatch):
    from radiology import auto_run
    monkeypatch.setattr(auto_run.TransferNowConnector, "interpretar", lambda *args: SimpleNamespace(
        original_filename="safe.zip", patient_name_candidate="SAFE", download_url="https://transfernow.net/dl/x"))
    messages = [SimpleNamespace(message_id=f"m{i}", text_body="x", html_body="", subject="x",
        received_at=datetime.now(timezone.utc)) for i in range(3)]
    history = IntakeHistoryRepository(tmp_path / "intake.db")
    _, summary = RadiologyAutoRunner(gmail=Messages(messages), downloader=BrowserRequired(),
        importer=Importer(), history=history, enabled=True, allow_copy=False, max_messages=3,
        summary_path=tmp_path / "summary.json", review_report_path=tmp_path / "review.txt").run()
    records = history.list_records(limit=10, status="REVIEW_REQUIRED")
    assert summary.review_required == len(records) == 3
    assert len({record.correlation_id for record in records}) == 3
    assert {record.run_id for record in records} == {summary.run_id}
    assert "Pendências radiológicas — dados sanitizados" in (tmp_path / "review.txt").read_text(encoding="utf-8")


def test_visible_mode_is_noninteractive_and_browser_failure_becomes_review(
    tmp_path, monkeypatch,
):
    configure_transfer(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *args: (_ for _ in ()).throw(
        AssertionError("input não deve ser solicitado")
    ))
    browser = FailedBrowser()
    history = IntakeHistoryRepository(tmp_path / "intake.db")
    _, summary = RadiologyAutoRunner(
        gmail=Messages([auto_message()]), downloader=BrowserRequired(),
        browser_downloader=browser, browser_mode="visible", importer=Importer(),
        history=history, enabled=True, allow_copy=False, max_messages=1,
        summary_path=tmp_path / "summary.json",
        review_report_path=tmp_path / "review.txt",
    ).run()
    record = history.list_records(status="REVIEW_REQUIRED")[0]
    assert browser.headless is False
    assert browser.allow_manual_interaction is False
    assert summary.review_required == 1
    assert (record.stage, record.reason_code) == (
        "DOWNLOAD", "BROWSER_AUTOMATION_FAILED"
    )


def test_visible_download_does_not_copy_when_allow_copy_is_false(
    tmp_path, monkeypatch,
):
    configure_transfer(monkeypatch)
    history = IntakeHistoryRepository(tmp_path / "intake.db")
    quarantine = tmp_path / "quarantine"
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    source_file = extracted / "image.dcm"
    source_file.write_bytes(b"dicom")
    patient_folder = tmp_path / "patients" / "SAFE"
    patient_folder.mkdir(parents=True)

    class Browser:
        headless = None
        allow_manual_interaction = True

        def download(self, *args):
            archive = quarantine / "correlation-visible" / "safe.zip"
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(b"archive")
            return DownloadResult(archive, 7, "a" * 64)

    class Extractor:
        def extract(self, path): return extracted

    class Patients:
        def find_candidates(self, name): return [Patient(1, "SAFE")]

    class AutomaticImporter(Importer):
        quarantine_root = quarantine
        extractor = Extractor()
        patient_repository = Patients()
        copy_called = False

        def register_download(self, *, archive_path, archive_sha256,
                              gmail_message_id, transfer_url):
            return history.create_downloaded(
                correlation_id=self.audit_logger.correlation_id,
                archive_filename=archive_path.name,
                archive_size=archive_path.stat().st_size,
                archive_sha256=archive_sha256,
                gmail_message_id=gmail_message_id,
                transfer_url=transfer_url,
            ).id

        def _source_files(self, path): return [source_file]
        def _choose_patient_folder(self, name, *, allow_auto=False):
            return patient_folder, "auto", "CONFIRMED_PATIENT_FOLDER"
        def _next_destination(self, folder): return folder / "EXAME"

        def _copy_and_manifest(self, **kwargs):
            self.copy_called = True
            raise AssertionError("cópia não permitida")

    importer = AutomaticImporter()
    _, summary = RadiologyAutoRunner(
        gmail=Messages([auto_message()]), downloader=BrowserRequired(),
        browser_downloader=Browser(), browser_mode="visible", importer=importer,
        history=history, enabled=True, allow_copy=False, max_messages=1,
        summary_path=tmp_path / "summary.json",
        review_report_path=tmp_path / "review.txt",
    ).run()
    assert importer.copy_called is False
    assert summary.review_required == 1
    assert history.list_records(status="REVIEW_REQUIRED")[0].reason_code == (
        "AUTO_COPY_DISABLED"
    )


def test_sqlite_diagnostic_uses_original_sanitized_exception():
    try:
        connection = __import__("sqlite3").connect(":memory:")
        connection.execute("SELECT missing_column FROM missing_table")
    except __import__("sqlite3").OperationalError as original:
        try:
            raise IntakeHistoryError(
                "consulta indisponível", operation="SELECT_MESSAGE_STATE"
            ) from original
        except IntakeHistoryError as wrapped:
            diagnostic = sanitized_history_failure(wrapped)
    assert diagnostic["function"] == "test_sqlite_diagnostic_uses_original_sanitized_exception"
    assert int(diagnostic["line"]) > 0
    assert diagnostic["sqlite_operation"] == "SELECT_MESSAGE_STATE"
    assert diagnostic["original_exception_type"] == "OperationalError"
    assert diagnostic["sqlite_code"] == str(__import__("sqlite3").SQLITE_ERROR)
    assert diagnostic["sanitized_original_message"] == "no such table: missing_table"


def test_history_failure_after_download_resumes_without_browser_download(
    tmp_path, monkeypatch,
):
    configure_transfer(monkeypatch)
    history = IntakeHistoryRepository(tmp_path / "intake.db")
    quarantine = tmp_path / "quarantine"

    class Browser:
        headless = True
        allow_manual_interaction = True
        calls = 0

        def download(self, url, filename, root, correlation_id, message_id):
            self.calls += 1
            archive = quarantine / correlation_id / "safe.zip"
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_bytes(b"archive")
            return DownloadResult(
                archive, 7, __import__("hashlib").sha256(b"archive").hexdigest()
            )

    class HistoryFailingImporter(Importer):
        quarantine_root = quarantine
        attempts = 0

        def register_download(self, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                try:
                    raise __import__("sqlite3").OperationalError("database is locked")
                except __import__("sqlite3").OperationalError as original:
                    raise IntakeHistoryError(
                        "histórico indisponível", operation="INSERT_DOWNLOADED"
                    ) from original
            raise RuntimeError("stop after checkpoint reuse")

    browser = Browser()
    importer = HistoryFailingImporter()

    def run_once(name):
        return RadiologyAutoRunner(
            gmail=Messages([auto_message()]), downloader=BrowserRequired(),
            browser_downloader=browser, browser_mode="headless", importer=importer,
            history=history, enabled=True, allow_copy=False, max_messages=1,
            summary_path=tmp_path / f"{name}.json",
            review_report_path=tmp_path / f"{name}.txt", debug=True,
        ).run()[1]

    first = run_once("first")
    second = run_once("second")
    assert first.reason_codes == ["HISTORY_UNAVAILABLE"]
    assert browser.calls == 1
    assert first.headless_download_completed == 1
    assert second.headless_download_completed == 0
    marker = next((quarantine / ".auto-run-state").glob("*.json"))
    marker_text = marker.read_text(encoding="utf-8")
    assert "safe.zip" not in marker_text
    assert "transfernow" not in marker_text
    assert str(quarantine) not in marker_text


def test_review_summary_counts_equivalent_pending_as_already_registered(tmp_path):
    history = IntakeHistoryRepository(tmp_path / "intake.db")
    first = history.create_downloaded(
        correlation_id="first-correlation", archive_filename="same.zip",
        archive_size=3, archive_sha256="e" * 64,
        gmail_message_id="same-message",
    )
    first = history.update(
        first.id, "READY_FOR_CONFIRMATION", patient_id_hash="same-patient"
    )
    history.set_review_required(
        first.id, reason_code="AUTO_COPY_DISABLED", stage="COPY"
    )
    repeated = history.create_downloaded(
        correlation_id="repeated-correlation", archive_filename="same.zip",
        archive_size=3, archive_sha256="e" * 64,
        gmail_message_id="same-message",
    )
    repeated = history.update(
        repeated.id, "READY_FOR_CONFIRMATION", patient_id_hash="same-patient"
    )
    summary = __import__("radiology.auto_run", fromlist=["new_summary"]).new_summary()
    runner = RadiologyAutoRunner(
        gmail=None, downloader=None, importer=Importer(), history=history,
        enabled=False, allow_copy=False, max_messages=1,
        summary_path=tmp_path / "summary.json",
        review_report_path=tmp_path / "review.txt",
    )
    runner._review(repeated.id, summary, "AUTO_COPY_DISABLED", "COPY")
    assert summary.skipped_already_registered == 1
    assert summary.review_required == 0
    assert len(history.list_records(status="REVIEW_REQUIRED")) == 1


def test_unexpected_error_is_failed_not_review(tmp_path):
    message = SimpleNamespace(message_id="m", received_at=datetime.now(timezone.utc))
    history = IntakeHistoryRepository(tmp_path / "intake.db")
    _, summary = RadiologyAutoRunner(gmail=Messages([message]), downloader=None, importer=Importer(),
        history=history, enabled=True, allow_copy=False, max_messages=1,
        summary_path=tmp_path / "summary.json", review_report_path=tmp_path / "review.txt").run()
    assert summary.failed == 1 and summary.review_required == 0
    assert history.list_records(status="FAILED")[0].reason_code == "UNEXPECTED_ERROR"


def test_global_failure_always_replaces_summary_atomically(tmp_path):
    output = tmp_path / "summary.json"
    history = IntakeHistoryRepository(tmp_path / "intake.db")
    runner = lambda: RadiologyAutoRunner(gmail=BrokenGmail(), downloader=None, importer=Importer(),
        history=history, enabled=True, allow_copy=False, max_messages=1, summary_path=output,
        review_report_path=tmp_path / "review.txt", debug=True)
    code, first = runner().run()
    first_payload = json.loads(output.read_text(encoding="utf-8"))
    code_again, second = runner().run()
    second_payload = json.loads(output.read_text(encoding="utf-8"))
    assert code == code_again == 1
    assert first.run_id != second.run_id != ""
    assert first_payload["run_id"] != second_payload["run_id"]
    assert second_payload["failed"] == 1 and second_payload["review_required"] == 0
    assert second_payload["global_failure"]["reason_code"] == "GLOBAL_AUTO_RUN_FAILURE"
    assert second_payload["global_failure"]["sanitized_message"] == "technical failure (RuntimeError)"
    assert not output.with_suffix(".json.tmp").exists()


def test_global_typeerror_debug_contains_only_sanitized_frame_details(tmp_path):
    class TypeErrorGmail:
        def list_messages(self, **kwargs):
            return len("invalid") + "x"
    output = tmp_path / "summary.json"
    history = IntakeHistoryRepository(tmp_path / "intake.db")
    _, summary = RadiologyAutoRunner(gmail=TypeErrorGmail(), downloader=None, importer=Importer(),
        history=history, enabled=True, allow_copy=False, max_messages=1, summary_path=output,
        review_report_path=tmp_path / "review.txt", debug=True).run()
    failure = json.loads(output.read_text(encoding="utf-8"))["global_failure"]
    assert summary.failed == 1 and summary.review_required == 0
    assert failure["exception_type"] == "TypeError"
    assert failure["function"] == "list_messages"
    assert failure["operation"] != "internal operation"
    assert "TypeErrorGmail" in failure["argument_types"]
    assert "invalid" not in json.dumps(failure)
