"""Testes do download temporário TransferNow e envio prático ao OneDrive."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path

import pytest
import requests

from integrations.onedrive_graph import (
    GraphFolder,
    GraphUploadedItem,
    OneDriveGraphError,
)
from integrations.transfernow_connector import TransferNowConnector
from radiology.transfernow_download import DownloadResult, TransferNowDownloadError
from scripts import test_transfernow_to_onedrive as transfer_script


class FakeResponse:
    def __init__(self, *, status=200, headers=None, chunks=(), text="") -> None:
        self.status_code = status
        self.headers = headers or {}
        self._chunks = list(chunks)
        self.text = text
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("internal secret URL")

    def iter_content(self, chunk_size):
        assert chunk_size == 1024 * 1024
        yield from self._chunks

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, *responses, error=None) -> None:
        self.responses = iter(responses)
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return next(self.responses)


def binary_response(filename="exame original.zip", content=b"arquivo"):
    return FakeResponse(
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(content)),
        },
        chunks=(content,),
    )


def test_valid_transfernow_url_is_accepted() -> None:
    url = "https://transfernow.net/dl/public-id"

    assert TransferNowConnector.validar_link_download(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://eviltransfernow.net/dl/id",
        "https://transfernow.net.evil.example/dl/id",
        "http://transfernow.net/dl/id",
        "https://example.org/?next=https://transfernow.net/dl/id",
    ],
)
def test_false_or_similar_domain_is_rejected(url: str) -> None:
    with pytest.raises(TransferNowDownloadError, match="inválido"):
        TransferNowConnector.validar_link_download(url)


def test_redirect_to_disallowed_domain_is_rejected() -> None:
    session = FakeSession(
        FakeResponse(
            status=302,
            headers={"Location": "https://malicious.example/archive.zip"},
        )
    )

    with pytest.raises(TransferNowDownloadError, match="Domínio"):
        with TransferNowConnector.baixar_temporariamente(
            "https://transfernow.net/dl/id", session=session
        ):
            pass

    assert len(session.calls) == 1


def test_download_timeout_is_sanitized() -> None:
    session = FakeSession(error=requests.Timeout("secret internal URL"))

    with pytest.raises(TransferNowDownloadError, match="Falha HTTP") as captured:
        with TransferNowConnector.baixar_temporariamente(
            "https://transfernow.net/dl/id", session=session
        ):
            pass

    assert "secret" not in str(captured.value)


def test_download_http_error_is_sanitized() -> None:
    session = FakeSession(FakeResponse(status=503))

    with pytest.raises(TransferNowDownloadError, match="Status HTTP") as captured:
        with TransferNowConnector.baixar_temporariamente(
            "https://transfernow.net/dl/id", session=session
        ):
            pass

    assert "secret" not in str(captured.value)


def test_empty_response_is_rejected() -> None:
    session = FakeSession(binary_response(content=b""))

    with pytest.raises(TransferNowDownloadError, match="vazio"):
        with TransferNowConnector.baixar_temporariamente(
            "https://transfernow.net/dl/id", session=session
        ):
            pass


def test_original_filename_is_preserved() -> None:
    session = FakeSession(binary_response(filename="Exame João.zip"))

    with TransferNowConnector.baixar_temporariamente(
        "https://transfernow.net/dl/id", session=session
    ) as downloaded:
        assert downloaded.path.name == "Exame João.zip"
        assert downloaded.path.read_bytes() == b"arquivo"


def test_temporary_download_is_cleaned_after_context() -> None:
    session = FakeSession(binary_response())

    with TransferNowConnector.baixar_temporariamente(
        "https://transfernow.net/dl/id", session=session
    ) as downloaded:
        downloaded_path = downloaded.path
        temporary_root = downloaded_path.parents[1]
        assert downloaded_path.is_file()

    assert not downloaded_path.exists()
    assert not temporary_root.exists()


class FakeAuth:
    def __init__(self, **_kwargs) -> None:
        pass

    def acquire_access_token(self):
        return "fixture-token"


def _temporary_download(local_file: Path):
    @contextmanager
    def download_context(*_args, **_kwargs):
        try:
            yield DownloadResult(
                local_file,
                local_file.stat().st_size,
                hashlib.sha256(local_file.read_bytes()).hexdigest(),
            )
        finally:
            local_file.unlink(missing_ok=True)

    return download_context


def _configure_script(monkeypatch, local_file: Path, graph_client) -> None:
    monkeypatch.setattr(
        transfer_script.TransferNowConnector,
        "validar_link_download",
        lambda _url: "https://transfernow.net/dl/validated",
    )
    monkeypatch.setattr(
        transfer_script.TransferNowConnector,
        "baixar_temporariamente",
        _temporary_download(local_file),
    )
    monkeypatch.setattr(transfer_script, "MicrosoftGraphAuth", FakeAuth)
    monkeypatch.setattr(transfer_script, "OneDriveGraphClient", graph_client)
    monkeypatch.setattr(transfer_script, "required_env", lambda _name: "fixture")


def test_transfernow_to_onedrive_upload_succeeds(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    local_file = tmp_path / "arquivo original.zip"
    local_file.write_bytes(b"arquivo")

    class SuccessfulGraphClient:
        MAX_SMALL_UPLOAD_BYTES = 250 * 1024 * 1024

        def __init__(self, _token):
            pass

        def get_authenticated_user(self):
            return object()

        def find_root_folder(self, _name):
            return GraphFolder("root-id", "Raiz", drive_id="drive-id")

        def ensure_folder(self, _root, name):
            assert name == "Teste TransferNow API"
            return GraphFolder("destination-id", name, drive_id="drive-id")

        def upload_small_file(self, _folder, path):
            assert path == local_file
            return GraphUploadedItem(path.name, path.stat().st_size, True)

    _configure_script(monkeypatch, local_file, SuccessfulGraphClient)

    assert transfer_script.run("https://transfernow.net/dl/id") == 0
    assert not local_file.exists()
    output = capsys.readouterr().out
    assert "Upload para OneDrive concluído: sim" in output
    assert "Arquivo remoto confirmado: sim" in output


def test_transfernow_to_onedrive_upload_failure_cleans_temporary_file(
    tmp_path: Path, monkeypatch
) -> None:
    local_file = tmp_path / "arquivo original.zip"
    local_file.write_bytes(b"arquivo")

    class FailingGraphClient:
        MAX_SMALL_UPLOAD_BYTES = 250 * 1024 * 1024

        def __init__(self, _token):
            pass

        def get_authenticated_user(self):
            return object()

        def find_root_folder(self, _name):
            return GraphFolder("root-id", "Raiz", drive_id="drive-id")

        def ensure_folder(self, _root, name):
            return GraphFolder("destination-id", name, drive_id="drive-id")

        def upload_small_file(self, _folder, _path):
            raise OneDriveGraphError("Falha segura no upload.")

    _configure_script(monkeypatch, local_file, FailingGraphClient)

    with pytest.raises(OneDriveGraphError, match="Falha segura"):
        transfer_script.run("https://transfernow.net/dl/id")

    assert not local_file.exists()
