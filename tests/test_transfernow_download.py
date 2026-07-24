"""Testes offline do download supervisionado TransferNow."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest
import requests

from models.email_message import EmailMessage
from radiology.gmail_import import run_gmail_import
from radiology.transfernow_download import (
    BrowserInteractionRequired,
    TransferNowDownloader,
    TransferNowDownloadError,
)


class FakeResponse:
    def __init__(self, *, status=200, headers=None, chunks=(), text="") -> None:
        self.status_code = status
        self.headers = headers or {}
        self._chunks = list(chunks)
        self.text = text
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("secret URL")

    def iter_content(self, chunk_size):
        assert chunk_size == 1024 * 1024
        for chunk in self._chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, *responses) -> None:
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


def downloader(session, **kwargs):
    return TransferNowDownloader(session=session, **kwargs)


def download(instance, tmp_path, filename="EXAME.zip"):
    return instance.download(
        "https://transfernow.net/dl/token",
        filename,
        tmp_path,
        "correlation-0001",
        "message-0001",
    )


def test_direct_download_checksum_part_and_request_contract(tmp_path: Path) -> None:
    content = b"arquivo-radiologico"
    session = FakeSession(
        FakeResponse(
            headers={"Content-Type": "application/zip", "Content-Length": str(len(content))},
            chunks=(content[:5], content[5:]),
        )
    )
    result = download(downloader(session), tmp_path)

    assert result.path.read_bytes() == content
    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert not result.path.with_suffix(".zip.part").exists()
    assert session.calls[0][1]["allow_redirects"] is False
    assert session.calls[0][1]["stream"] is True
    assert session.calls[0][1]["timeout"] == (30, 1800)
    assert session.calls[0][1]["headers"]["User-Agent"]


def test_allowed_redirect_is_followed_manually(tmp_path: Path) -> None:
    session = FakeSession(
        FakeResponse(status=302, headers={"Location": "https://cdn.transfernow.net/download/file"}),
        FakeResponse(headers={"Content-Type": "application/zip"}, chunks=(b"ok",)),
    )
    result = download(downloader(session), tmp_path)
    assert result.size_bytes == 2
    assert [call[0] for call in session.calls] == [
        "https://transfernow.net/dl/token",
        "https://cdn.transfernow.net/download/file",
    ]


def test_malicious_redirect_is_blocked_before_request(tmp_path: Path) -> None:
    session = FakeSession(
        FakeResponse(status=302, headers={"Location": "https://evil.example/file.zip"})
    )
    with pytest.raises(TransferNowDownloadError, match="Domínio"):
        download(downloader(session), tmp_path)
    assert len(session.calls) == 1


def test_html_landing_page_uses_safe_download_link(tmp_path: Path) -> None:
    session = FakeSession(
        FakeResponse(
            headers={"Content-Type": "text/html; charset=utf-8"},
            text='<a href="https://files.transfernow.net/download/archive">Download</a>',
        ),
        FakeResponse(headers={"Content-Type": "application/zip"}, chunks=(b"zip",)),
    )
    assert download(downloader(session), tmp_path).size_bytes == 3


def test_html_without_reliable_endpoint_requires_browser(tmp_path: Path) -> None:
    session = FakeSession(
        FakeResponse(headers={"Content-Type": "text/html"}, text="<script>download()</script>")
    )
    with pytest.raises(BrowserInteractionRequired, match="--archive-path"):
        download(downloader(session), tmp_path)


def test_content_disposition_defines_safe_filename(tmp_path: Path) -> None:
    session = FakeSession(
        FakeResponse(
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Disposition": "attachment; filename=RADIOLOGIA.rar",
            },
            chunks=(b"rar",),
        )
    )
    result = download(downloader(session), tmp_path)
    assert result.path.name == "RADIOLOGIA.rar"


def test_long_patient_filename_is_truncated_safely_and_keeps_rar(tmp_path: Path) -> None:
    original = f"{'PACIENTE COM NOME COMPLETO MUITO LONGO ' * 8}20260714.rar"
    session = FakeSession(
        FakeResponse(headers={"Content-Type": "application/octet-stream"}, chunks=(b"rar",))
    )
    result = download(downloader(session), tmp_path, original)
    assert len(result.path.name) == TransferNowDownloader.MAX_FILENAME_LENGTH
    assert result.path.name.endswith(".rar")
    assert "PACIENTE COM NOME COMPLETO" in result.path.name


def test_accents_spaces_and_windows_invalid_characters_are_sanitized(tmp_path: Path) -> None:
    session = FakeSession(
        FakeResponse(headers={"Content-Type": "application/octet-stream"}, chunks=(b"rar",))
    )
    result = download(downloader(session), tmp_path, 'JOÃO ÁLVARO: EXAME? FINAL.rar')
    assert result.path.name == "JOÃO ÁLVARO_ EXAME_ FINAL.rar"


def test_content_disposition_overrides_full_email_filename(tmp_path: Path) -> None:
    session = FakeSession(
        FakeResponse(
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Disposition": "attachment; filename*=UTF-8''LAUDO%20FINAL.zip",
            },
            chunks=(b"zip",),
        )
    )
    result = download(downloader(session), tmp_path, "NOME COMPLETO DO PACIENTE.rar")
    assert result.path.name == "LAUDO FINAL.zip"


@pytest.mark.parametrize(
    "filename",
    ("../EXAME.zip", "pasta/EXAME.zip", r"pasta\EXAME.rar", "virus.exe", "..zip"),
)
def test_invalid_or_traversal_filename_is_rejected(tmp_path: Path, filename: str) -> None:
    session = FakeSession(
        FakeResponse(headers={"Content-Type": "application/octet-stream"}, chunks=(b"data",))
    )
    with pytest.raises(TransferNowDownloadError):
        download(downloader(session), tmp_path, filename)
    assert len(session.calls) == 1


@pytest.mark.parametrize("filename", ("PACIENTE_20... .rar", "NOME TRUNCADO....zip"))
def test_display_or_truncated_filename_is_never_accepted(tmp_path: Path, filename: str) -> None:
    session = FakeSession(
        FakeResponse(headers={"Content-Type": "application/octet-stream"}, chunks=(b"data",))
    )
    with pytest.raises(TransferNowDownloadError, match="Nome de arquivo inválido"):
        download(downloader(session), tmp_path, filename)


def test_download_without_content_length_is_accepted(tmp_path: Path) -> None:
    session = FakeSession(FakeResponse(headers={"Content-Type": "application/zip"}, chunks=(b"data",)))
    assert download(downloader(session), tmp_path).size_bytes == 4


def test_empty_download_keeps_part_and_failed_status(tmp_path: Path) -> None:
    session = FakeSession(FakeResponse(headers={"Content-Type": "application/zip"}, chunks=()))
    with pytest.raises(TransferNowDownloadError, match="vazio"):
        download(downloader(session), tmp_path)
    folder = tmp_path / "correlation-0001"
    assert (folder / "EXAME.zip.part").is_file()
    assert json.loads((folder / "EXAME.zip.download.json").read_text())["status"] == "FAILED"


def test_partial_download_keeps_part(tmp_path: Path) -> None:
    session = FakeSession(
        FakeResponse(
            headers={"Content-Type": "application/zip", "Content-Length": "10"},
            chunks=(b"short",),
        )
    )
    with pytest.raises(TransferNowDownloadError, match="incompleto"):
        download(downloader(session), tmp_path)
    assert (tmp_path / "correlation-0001" / "EXAME.zip.part").read_bytes() == b"short"


def test_timeout_is_sanitized_and_part_is_preserved(tmp_path: Path) -> None:
    session = FakeSession(
        FakeResponse(
            headers={"Content-Type": "application/zip"},
            chunks=(b"partial", requests.Timeout("https://secret")),
        )
    )
    with pytest.raises(TransferNowDownloadError, match="interrompido") as captured:
        download(downloader(session), tmp_path)
    assert "secret" not in str(captured.value)
    assert (tmp_path / "correlation-0001" / "EXAME.zip.part").is_file()


def test_content_length_and_stream_limit_are_enforced(tmp_path: Path) -> None:
    session = FakeSession(
        FakeResponse(headers={"Content-Length": "4", "Content-Type": "application/zip"})
    )
    with pytest.raises(TransferNowDownloadError, match="limite"):
        download(downloader(session, max_download_bytes=3), tmp_path)

    other = tmp_path / "other"
    session = FakeSession(FakeResponse(headers={"Content-Type": "application/zip"}, chunks=(b"1234",)))
    with pytest.raises(TransferNowDownloadError, match="limite"):
        download(downloader(session, max_download_bytes=3), other)
    assert (other / "correlation-0001" / "EXAME.zip.part").is_file()


def test_completed_download_is_idempotently_reused(tmp_path: Path) -> None:
    first_session = FakeSession(FakeResponse(headers={"Content-Type": "application/zip"}, chunks=(b"same",)))
    first = download(downloader(first_session), tmp_path)
    second_session = FakeSession(
        FakeResponse(headers={"Content-Type": "application/zip"}, chunks=())
    )
    second = download(downloader(second_session), tmp_path)
    assert second == type(first)(first.path, first.size_bytes, first.sha256, True)
    assert len(second_session.calls) == 1


def test_existing_untracked_file_requires_human_review(tmp_path: Path) -> None:
    folder = tmp_path / "correlation-0001"
    folder.mkdir()
    (folder / "EXAME.zip").write_bytes(b"existing")
    session = FakeSession(
        FakeResponse(headers={"Content-Type": "application/zip"}, chunks=())
    )
    with pytest.raises(TransferNowDownloadError, match="revisão humana"):
        download(downloader(session), tmp_path)


def test_gmail_orchestration_is_readonly_and_reuses_supervised_import(tmp_path: Path) -> None:
    message = EmailMessage(
        message_id="real-id-secret",
        subject='TransferNow "PACIENTE_20991231.zip"',
        sender="TransferNow <noreply@transfernow.net>",
        reply_to=None,
        received_at=datetime(2099, 12, 31, tzinfo=timezone.utc),
        text_body=(
            "clinica@institucional.example enviou. Tamanho: 636 MB. "
            "Válido: 31/12/2099 https://transfernow.net/dl/token"
        ),
    )

    class ReadOnlyGmail:
        def __init__(self):
            self.calls = []

        def list_messages(self, **kwargs):
            self.calls.append(kwargs)
            return [message]

    class FakeDownloader:
        def download(self, url, filename, root, correlation_id, message_id):
            path = tmp_path / filename
            path.write_bytes(b"zip")
            from radiology.transfernow_download import DownloadResult
            return DownloadResult(path, 3, hashlib.sha256(b"zip").hexdigest())

    class FakeImporter:
        def __init__(self):
            self.paths = []

        def run(self, *, archive_path):
            self.paths.append(archive_path)
            return object()

    gmail = ReadOnlyGmail()
    importer = FakeImporter()
    outputs = []
    answers = iter(("1", "confirmar", "confirmar"))
    outcome = run_gmail_import(
        gmail=gmail,
        downloader=FakeDownloader(),
        importer=importer,
        quarantine_root=tmp_path,
        correlation_id="correlation-0001",
        input_func=lambda _: next(answers),
        output=outputs.append,
    )
    assert outcome.import_result is not None
    assert gmail.calls == [{
        "query": "from:noreply@transfernow.net subject:TransferNow",
        "max_results": 5,
    }]
    assert importer.paths == [outcome.download.path]
    assert all("https://" not in line and "real-id-secret" not in line for line in outputs)


def test_gmail_display_filename_is_separate_from_original_download_name(tmp_path: Path) -> None:
    original = "JULIANO GENYSON DE OLIVEIRA_20260714153000.rar"
    message = EmailMessage(
        message_id="secret-id",
        subject=f'TransferNow "{original}"',
        sender="TransferNow <noreply@transfernow.net>",
        reply_to=None,
        text_body="clinica@example.org https://transfernow.net/dl/token",
    )

    class Gmail:
        def list_messages(self, **kwargs): return [message]

    received = []

    class Downloader:
        def download(self, url, original_filename, root, correlation_id, message_id):
            received.append(original_filename)
            path = tmp_path / original_filename
            path.write_bytes(b"rar")
            from radiology.transfernow_download import DownloadResult
            return DownloadResult(path, 3, hashlib.sha256(b"rar").hexdigest())

    outputs = []

    class Importer:
        def run(self, *, archive_path):
            return archive_path

    outcome = run_gmail_import(
        gmail=Gmail(), downloader=Downloader(), importer=Importer(),
        quarantine_root=tmp_path, correlation_id="correlation-0001",
        input_func=lambda prompt: pytest.fail(f"prompt inesperado: {prompt}"),
        output=outputs.append,
    )
    assert received == [original]
    assert any("... .rar" in line for line in outputs)
    assert original not in outputs[1]
    assert original not in outputs[2]
    assert outcome.download.path.name == original
    assert outcome.download.path.suffix == ".rar"


def test_pipeline_continues_automatically_after_download(tmp_path: Path, monkeypatch) -> None:
    message = EmailMessage(
        message_id="id",
        subject='TransferNow "PACIENTE_20991231.zip"',
        sender="TransferNow",
        reply_to=None,
        text_body="sender@example.org https://transfernow.net/dl/token",
    )

    class Gmail:
        def list_messages(self, **kwargs): return [message]

    class Downloader:
        def download(self, *args):
            from radiology.transfernow_download import DownloadResult
            path = tmp_path / "PACIENTE_20991231.zip"
            path.write_bytes(b"kept")
            return DownloadResult(path, 4, hashlib.sha256(b"kept").hexdigest())

    calls = []

    class RecordingImporter:
        def run(self, **kwargs):
            calls.append(kwargs)
            return "published"

    monkeypatch.setattr(Path, "unlink", lambda *args: (_ for _ in ()).throw(AssertionError("delete")))
    outcome = run_gmail_import(
        gmail=Gmail(), downloader=Downloader(), importer=RecordingImporter(),
        quarantine_root=tmp_path, correlation_id="correlation-0001",
        input_func=lambda prompt: pytest.fail(f"prompt inesperado: {prompt}"),
        output=lambda _: None,
    )
    assert outcome.import_result == "published"
    assert calls[0]["archive_path"] == outcome.download.path
    assert outcome.download.path.read_bytes() == b"kept"
