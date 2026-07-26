"""Testes offline da camada multiprovedor e do Cfaz."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
import requests

from acquisition.cfaz_provider import (
    CfazAmbiguousRequestError, CfazAuthenticationError, CfazEmptyRequestError,
    CfazProvider, CfazRequestError,
)
from acquisition.base import AssetClassification
from acquisition.service import ProviderAcquisitionService
from acquisition.transfernow_provider import TransferNowProvider
from models.email_message import EmailMessage
from radiology.transfernow_download import DownloadResult
from tests.synthetic_fixtures import (
    SYNTHETIC_CPF_PLACEHOLDER,
    SYNTHETIC_PERSON_ACCENTED,
    SYNTHETIC_TIMESTAMP,
)


class Response:
    def __init__(self, *, status=200, payload=None, headers=None, content=b"", cookies=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.content = content
        self.cookies = cookies or {}

    def json(self):
        if isinstance(self._payload, Exception): raise self._payload
        return self._payload

    def iter_content(self, size):
        yield self.content


class Session:
    def __init__(self, *, request_payload=None, files=None, login=None):
        self.request_payload = request_payload or {}
        self.files = files or {}
        self.login = login
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if isinstance(self.login, Exception): raise self.login
        return self.login or Response(status=401, payload={})

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/api/v1/requests/99999991"):
            if isinstance(self.request_payload, Exception): raise self.request_payload
            return Response(payload=self.request_payload)
        value = self.files[url]
        if isinstance(value, Exception): raise value
        return Response(content=value)


def jpeg(width: int, height: int, marker: bytes = b"x") -> bytes:
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof = (b"\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big")
           + width.to_bytes(2, "big") + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00")
    return b"\xff\xd8" + app0 + sof + marker + b"\xff\xd9"


def notification():
    return EmailMessage(
        message_id="gmail-secret", subject="Seu pedido Cfaz está disponível",
        sender="Cfaz <notificacao@cfaz.net>", reply_to=None,
        received_at=datetime(2026, 3, 10, tzinfo=timezone.utc),
        text_body="Acesse https://max.cfaz.net/requests/99999991 para consultar.",
    )


def order_payload(*, assets=True):
    return {
        "id": 99999991,
        "created_at": "2026-03-10T11:00:00-03:00",
        "date": "2026-03-10T11:10:13-03:00",
        "patient_datum": {"name": SYNTHETIC_PERSON_ACCENTED},
        "dentist_datum": {"name": "Dra. Solicitante"},
        "clinic": {"name": "Radiologia Exemplo"},
        "images_download_links": ([
            "https://files.cfaz.net/panoramica.jpg",
            "https://files.cfaz.net/telerradiografia.png",
        ] if assets else []),
        "reports": ([{
            "id": 99, "download_url": "https://files.cfaz.net/laudo.pdf",
            "name": "laudo.pdf",
        }] if assets else []),
    }


def test_authentication_supports_official_token_and_dentist_session() -> None:
    token_session = Session(request_payload=order_payload())
    token = CfazProvider(api_token="secret-token", session=token_session)
    token.authenticate()
    token.discover(notification())
    headers = token_session.calls[0][2]["headers"]
    assert headers["Authorization"] == "Token secret-token"

    login = Response(headers={
        "access-token": "secret-access", "client": "client-secret",
        "uid": "fixture1@example.com", "expiry": "999999", "token-type": "Bearer",
    })
    password_session = Session(login=login)
    provider = CfazProvider(
        email="fixture1@example.com", password="never-log", session=password_session
    )
    provider.authenticate()
    call = password_session.calls[0]
    assert call[2]["data"]["password"] == "never-log"
    assert "never-log" not in repr(provider._auth_headers)


def test_fixed_api_token_uses_exact_official_authorization_header() -> None:
    session = Session(request_payload=order_payload())
    provider = CfazProvider(api_token="  abc123  ", session=session)

    provider.discover_request("99999991")

    assert session.calls[0][2]["headers"] == {"Authorization": "Token abc123"}
    for invalid in ('"abc123"', "abc 123", "abc\n123", "Token abc123"):
        with pytest.raises(CfazAuthenticationError, match="token bruto"):
            CfazProvider(api_token=invalid, session=Session()).authenticate()


def test_discovers_request_metadata_all_assets_and_classifications() -> None:
    provider = CfazProvider(
        api_token="token", session=Session(request_payload=order_payload())
    )
    request = provider.discover(notification())[0]

    assert request.request_id == "99999991"
    assert request.patient_name == SYNTHETIC_PERSON_ACCENTED
    assert request.radiology_clinic == "Radiologia Exemplo"
    assert request.professional == "Dra. Solicitante"
    assert request.source_url == "https://max.cfaz.net/requests/99999991"
    assert len(request.assets) == 3
    assert {item.classification.value for item in request.assets} == {
        "Imagem", "Laudo",
    }


def test_discovers_explicit_request_directly_without_gmail_notification() -> None:
    session = Session(request_payload=order_payload())
    provider = CfazProvider(api_token="token", session=session)

    request = provider.discover_request("99999991")[0]

    assert request.request_id == "99999991"
    assert request.provider_request_id == "99999991"
    assert session.calls[0][1].endswith("/api/v1/requests/99999991")
    with pytest.raises(CfazRequestError, match="Request ID.*inválido"):
        provider.discover_request("99999991?token=secret")


def test_visible_sequential_id_is_resolved_by_documented_paginated_listing() -> None:
    output = []
    detail = order_payload()
    detail.update({
        "id": 99999992,
        "sequential_id": 999991,
        "clinic": {"sequential_id": 999992, "name": "must not appear in debug"},
    })

    class LookupSession:
        cookies = {}
        def __init__(self): self.calls = []
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if url.endswith("/requests/999991"):
                return Response(status=404, payload={"message": "not found"})
            if url.endswith("/requests/99999992"):
                return Response(payload=detail)
            page = kwargs["params"]["page"]
            if page == 1:
                return Response(payload={
                    "data": [{"id": 1, "sequential_id": 10}],
                    "pagination": {"total_pages": 2},
                })
            return Response(payload={
                "data": [{"id": 99999992, "sequential_id": 999991}],
                "pagination": {"total_pages": 2},
            })

    session = LookupSession()
    provider = CfazProvider(
        api_token="abc123", session=session, payload_diagnostics=True,
        output=output.append,
        now_provider=lambda: datetime(2026, 7, 24, tzinfo=timezone.utc),
    )

    request = provider.discover_request("999991")[0]

    assert request.request_id == "99999992"
    assert request.provider_request_id == "99999992"
    assert request.sequential_id == "999991"
    assert request.clinic_number == "999992"
    assert [call[0].rsplit("/", 1)[-1] for call in session.calls] == [
        "999991", "requests", "requests", "99999992",
    ]
    list_params = session.calls[1][1]["params"]
    assert list_params["page"] == 1 and list_params["per_page"] == 100
    assert list_params["q[created_at_gteq]"].startswith("2025-07-24")
    text = "\n".join(output)
    assert "Pedido visível: 999991" in text
    assert "ID interno resolvido: 99999992" in text
    assert "Nº Clínica: 999992" in text
    assert "must not appear" not in text


@pytest.mark.parametrize("mode", ["missing", "ambiguous"])
def test_sequential_id_missing_or_ambiguous_never_imports(mode) -> None:
    class LookupSession:
        cookies = {}
        def get(self, url, **kwargs):
            if url.endswith("/requests/999991"):
                return Response(status=404, payload={})
            items = [] if mode == "missing" else [
                {"id": 100, "sequential_id": 999991},
                {"id": 101, "sequential_id": 999991},
            ]
            return Response(payload={
                "data": items, "pagination": {"total_pages": 1},
            })

    provider = CfazProvider(api_token="abc123", session=LookupSession())
    expected = CfazAmbiguousRequestError if mode == "ambiguous" else CfazRequestError
    with pytest.raises(expected):
        provider.discover_request("999991")


def test_payload_diagnostics_accept_anonymized_real_nested_structure() -> None:
    output = []
    payload = {
        "data": {
            "id": 99999991,
            "patient_datum": {
                "name": "PATIENT MUST NOT LEAK",
                "cpf": SYNTHETIC_CPF_PLACEHOLDER,
            },
            "archives": [],
            "images_download_links": [],
            "reports": [{
                "id": 999993,
                "associated_images_download_links": [],
                "result_document": (
                    "https://files.cfaz.net/results/report.pdf?token=SIGNED-SECRET"
                ),
                "text": "REPORT CONTENT MUST NOT LEAK",
            }],
            "tomographies": [{
                "id": 999994,
                "tomography_files": [{
                    "document_url": (
                        "https://storage.googleapis.com/bucket/volume.zip?signature=SECRET"
                    )
                }],
            }],
            "link_token": (
                "https://max.cfaz.net/requests_with_token/99999991?access_token=SECRET"
            ),
        }
    }
    provider = CfazProvider(
        api_token="abc123", session=Session(request_payload=payload),
        payload_diagnostics=True, output=output.append,
    )

    request = provider.discover_request("99999991")[0]

    assert len(request.assets) == 2
    text = "\n".join(output)
    assert "Chaves de primeiro nível: data" in text
    assert "Campo raiz data: dict, quantidade=7" in text
    assert "Quantidade de exames: 2" in text
    assert "Coleção archives: list, quantidade=0" in text
    assert "Exame reports[1] chaves:" in text
    assert "Exame tomographies[1] chaves:" in text
    assert "domínio=files.cfaz.net" in text
    assert "domínio=storage.googleapis.com" in text
    assert "Endpoint separado por exam_id: não documentado publicamente" in text
    for sensitive in (
        "PATIENT MUST NOT LEAK", SYNTHETIC_CPF_PLACEHOLDER, "SIGNED-SECRET",
        "REPORT CONTENT MUST NOT LEAK", "signature=", "access_token=SECRET",
    ):
        assert sensitive not in text


def test_empty_diagnostics_distinguish_exams_without_files_and_denied_access(tmp_path) -> None:
    payload = order_payload(assets=False)
    payload["tomographies"] = [{"id": 999994, "tomography_files": []}]
    provider = CfazProvider(
        api_token="abc123", session=Session(request_payload=payload),
        payload_diagnostics=True, output=lambda _: None,
    )
    request = provider.discover_request("99999991")[0]
    with pytest.raises(
        CfazEmptyRequestError, match="Exames existentes sem arquivos"
    ):
        provider.download(request, tmp_path, "correlation")

    output = []

    class ForbiddenSession:
        cookies = {}
        def get(self, *args, **kwargs):
            return Response(status=403, payload={"message": "forbidden"})

    denied = CfazProvider(
        api_token="abc123", session=ForbiddenSession(),
        payload_diagnostics=True, output=output.append,
    )
    with pytest.raises(CfazRequestError, match="HTTP 403"):
        denied.discover_request("99999991")
    assert "Acesso ao pedido/arquivos: negado (HTTP 403)" in output


def test_auth_diagnostics_report_presence_and_never_credential_values() -> None:
    output = []
    session = Session(request_payload=order_payload())
    provider = CfazProvider(
        api_token="super-secret-api-token", session=session,
        auth_diagnostics=True, output=output.append,
    )

    provider.discover_request("99999991")

    text = "\n".join(output)
    assert "Login URL: não aplicável" in text
    assert "Token recebido: sim" in text
    assert "Authorization configurado: sim" in text
    assert "Tipo de autenticação: Token" in text
    assert "Consulta URL: https://max.cfaz.net/api/v1/requests/99999991" in text
    assert "Cabeçalho Authorization presente: sim" in text
    assert "Cookies enviados: não" in text
    assert "Consulta HTTP Status: 200" in text
    assert "Tentativa 1: header Token -> HTTP 200" in text
    assert "Authorization scheme: Token" in text
    assert "Authorization token length: 22" in text
    assert "Authorization token fingerprint:" in text
    assert "super-secret-api-token" not in text


def test_fixed_token_401_retries_once_with_sanitized_query_fallback() -> None:
    output = []

    class FallbackSession:
        cookies = {}

        def __init__(self): self.calls = []
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if len(self.calls) == 1:
                return Response(status=401, payload={"message": "unauthorized"})
            return Response(status=200, payload=order_payload())

    session = FallbackSession()
    provider = CfazProvider(
        api_token="abc123", session=session,
        auth_diagnostics=True, output=output.append,
    )

    provider.discover_request("99999991")

    assert len(session.calls) == 2
    assert session.calls[0][1] == {
        "headers": {"Authorization": "Token abc123"},
        "timeout": (10.0, 30.0),
    }
    assert session.calls[1][1] == {
        "headers": {}, "params": {"access_token": "abc123"},
        "timeout": (10.0, 30.0),
    }
    text = "\n".join(output)
    assert "Tentativa 1: header Token -> HTTP 401" in text
    assert "Tentativa 2: query access_token -> HTTP 200" in text
    assert "?access_token=" not in text
    assert "abc123" not in text
    assert "Authorization token length: 6" in text
    assert (
        "Authorization token fingerprint: "
        + hashlib.sha256(b"abc123").hexdigest()[:8]
    ) in text

    normal_session = FallbackSession()
    CfazProvider(
        api_token="abc123", session=normal_session
    ).discover_request("99999991")
    # O fallback é parte da autenticação funcional, não depende do modo debug.
    assert len(normal_session.calls) == 2
    assert normal_session.calls[1][1]["params"] == {
        "access_token": "abc123"
    }


def test_login_and_401_diagnostics_sanitize_json_tokens_cookies_and_credentials() -> None:
    output = []

    class UnauthorizedSession(Session):
        cookies = {"session": "cookie-secret"}

        def get(self, url, **kwargs):
            self.calls.append(("GET", url, kwargs))
            return Response(status=401, payload={
                "message": "Bearer response-secret não autorizado",
                "code": "AUTH_401",
                "access_token": "response-secret",
                "nested": {"cookie": "cookie-secret"},
            })

    login = Response(headers={
        "Content-Type": "application/json; charset=utf-8",
        "Set-Cookie": "session=cookie-secret",
        "access-token": "login-secret", "client": "client-secret",
        "uid": "fixture1@example.com", "expiry": "999999",
        "token-type": "Bearer",
    }, cookies={"session": "cookie-secret"})
    session = UnauthorizedSession(login=login)
    provider = CfazProvider(
        email="fixture1@example.com", password="password-secret",
        session=session, auth_diagnostics=True, output=output.append,
    )

    with pytest.raises(CfazRequestError, match="HTTP 401"):
        provider.discover_request("99999991")

    text = "\n".join(output)
    assert "Login HTTP Status: 200" in text
    assert "Login Content-Type: application/json" in text
    assert "Token recebido: sim" in text
    assert "Cookie recebido: sim" in text
    assert "Authorization configurado: não" in text
    assert "Tipo de autenticação: Session" in text
    assert "Cookies enviados: sim" in text
    assert "Consulta HTTP Status: 401" in text
    assert '"code": "AUTH_401"' in text
    assert "Mensagem da API: Bearer [REDACTED] não autorizado" in text
    assert "Código interno da API: AUTH_401" in text
    for secret in ("password-secret", "login-secret", "client-secret",
                   "response-secret", "cookie-secret"):
        assert secret not in text


def test_empty_request_login_failure_and_timeout_are_sanitized(tmp_path) -> None:
    empty = CfazProvider(
        api_token="token", session=Session(request_payload=order_payload(assets=False))
    )
    request = empty.discover(notification())[0]
    with pytest.raises(CfazEmptyRequestError, match="não possui arquivos"):
        empty.download(request, tmp_path, "correlation-123")

    failed = CfazProvider(email="user", password="top-secret", session=Session())
    with pytest.raises(CfazAuthenticationError) as captured:
        failed.authenticate()
    assert "top-secret" not in str(captured.value)

    timeout = CfazProvider(
        api_token="token", session=Session(request_payload=requests.Timeout())
    )
    with pytest.raises(CfazRequestError, match="Tempo limite"):
        timeout.discover(notification())


def test_downloads_multiple_files_packages_deterministically_and_resumes(tmp_path) -> None:
    files = {
        "https://files.cfaz.net/panoramica.jpg": b"pan",
        "https://files.cfaz.net/telerradiografia.png": b"tele",
        "https://files.cfaz.net/laudo.pdf": b"report",
    }
    session = Session(request_payload=order_payload(), files=files)
    provider = CfazProvider(api_token="token", session=session)
    request = provider.discover(notification())[0]

    first = provider.download(request, tmp_path, "correlation-123")
    first_sha = first.sha256
    file_gets = len([call for call in session.calls if call[1] in files])
    second = provider.download(request, tmp_path, "correlation-123")

    assert first.file_count == 3 and first.total_bytes == 13
    assert second.resumed_files == 3
    assert second.sha256 == first_sha
    assert ProviderAcquisitionService._stable_exam_id(first) == (
        ProviderAcquisitionService._stable_exam_id(second)
    )
    assert len([call for call in session.calls if call[1] in files]) == file_gets
    with zipfile.ZipFile(first.archive_path) as archive:
        assert sorted(archive.namelist()) == [
            "laudo_001.pdf", "radiografia_001.jpg", "radiografia_002.png"
        ]
    state = (first.archive_path.parent / ".cfaz-download-state.json").read_text("utf-8")
    assert "secret" not in state and "https://" not in state


def test_real_url_lists_html_view_link_content_dedup_and_signed_url_idempotency(
    tmp_path,
) -> None:
    root_one = "https://storage.googleapis.com/bucket/root-one?signature=ROOT-SECRET"
    root_two = "https://storage.googleapis.com/bucket/root-two?signature=SECOND-SECRET"
    duplicate = "https://storage.googleapis.com/bucket/duplicate?signature=DUP-SECRET"
    associated = "https://storage.googleapis.com/bucket/report-image?signature=REPORT-SECRET"
    report_link = "https://max.cfaz.net/reports/991/edit?access_token=LINK-SECRET"
    payload = order_payload(assets=False)
    payload["images_download_links"] = [
        root_one,
        {"nested": [{"download": root_two}]},
    ]
    payload["reports"] = [{
        "id": 991,
        "associated_images_download_links": [duplicate, associated],
        "link": report_link,
    }]
    jpeg = b"\xff\xd8\xff" + b"jpeg-content"
    png = b"\x89PNG\r\n\x1a\n" + b"png-content"
    report_png = b"\x89PNG\r\n\x1a\n" + b"report-content"

    class RealSession(Session):
        def get(self, url, **kwargs):
            self.calls.append(("GET", url, kwargs))
            if url.endswith("/api/v1/requests/99999991"):
                return Response(payload=payload)
            responses = {
                root_one: Response(
                    content=jpeg, headers={"Content-Type": "application/octet-stream"}
                ),
                root_two: Response(content=png, headers={"Content-Type": "image/png"}),
                duplicate: Response(content=jpeg, headers={"Content-Type": "image/jpeg"}),
                associated: Response(
                    content=report_png, headers={"Content-Type": "image/png"}
                ),
                report_link: Response(
                    content=b"<html>viewer</html>",
                    headers={"Content-Type": "text/html; charset=utf-8"},
                ),
            }
            return responses[url]

    session = RealSession()
    provider = CfazProvider(api_token="abc123", session=session)
    request = provider.discover_request("99999991")[0]

    assert len(request.assets) == 5
    assert sum(
        asset.classification == AssetClassification.REPORT_ASSOCIATED_IMAGE
        for asset in request.assets
    ) == 2
    first = provider.download(request, tmp_path, "correlation-real")

    assert first.file_count == 3
    assert first.total_bytes == len(jpeg) + len(png) + len(report_png)
    assert {asset.classification for asset in first.request.assets} == {
        AssetClassification.IMAGE,
        AssetClassification.REPORT_ASSOCIATED_IMAGE,
    }
    with zipfile.ZipFile(first.archive_path) as archive:
        assert len(archive.namelist()) == 3
        assert hashlib.sha256(jpeg).hexdigest() in {
            hashlib.sha256(archive.read(name)).hexdigest()
            for name in archive.namelist()
        }
    state_path = first.archive_path.parent / ".cfaz-download-state.json"
    state_text = state_path.read_text("utf-8")
    for sensitive in (
        "ROOT-SECRET", "SECOND-SECRET", "DUP-SECRET", "REPORT-SECRET",
        "LINK-SECRET", "https://", "storage.googleapis.com",
    ):
        assert sensitive not in state_text

    download_calls = len(session.calls)
    payload["images_download_links"][0] = (
        "https://storage.googleapis.com/bucket/root-one?signature=ROTATED-SECRET"
    )
    second_request = provider.discover_request("99999991")[0]
    second = provider.download(second_request, tmp_path, "correlation-real")
    assert second.sha256 == first.sha256
    assert second.file_count == 3
    # Só a nova consulta do pedido ocorre; nenhum arquivo ou viewer é requisitado novamente.
    assert len(session.calls) == download_calls + 1


def test_cfaz_download_rejects_declared_or_streamed_size_over_limit(tmp_path) -> None:
    url = "https://storage.googleapis.com/bucket/large?signature=SECRET"
    payload = order_payload(assets=False)
    payload["images_download_links"] = url

    class LargeSession(Session):
        def get(self, request_url, **kwargs):
            if request_url.endswith("/api/v1/requests/99999991"):
                return Response(payload=payload)
            return Response(
                content=b"\x89PNG\r\n\x1a\n12345",
                headers={"Content-Type": "image/png", "Content-Length": "13"},
            )

    provider = CfazProvider(
        api_token="abc123", session=LargeSession(), max_file_bytes=8,
    )
    request = provider.discover_request("99999991")[0]
    with pytest.raises(CfazRequestError, match="excede o limite"):
        provider.download(request, tmp_path, "correlation-large")


def test_extensionless_jpeg_gets_readable_name_dimensions_and_thumbnail_filter(tmp_path) -> None:
    original_url = "https://storage.googleapis.com/bucket/original?signature=SECRET"
    thumbnail_url = "https://storage.googleapis.com/bucket/thumb?signature=SECRET"
    other_url = "https://storage.googleapis.com/bucket/other?signature=SECRET"
    payload = order_payload(assets=False)
    payload["images_download_links"] = [original_url]
    payload["reports"] = [{
        "id": 9,
        "associated_images_download_links": [thumbnail_url, other_url],
    }]
    files = {
        original_url: jpeg(1200, 800, b"original"),
        thumbnail_url: jpeg(300, 200, b"thumbnail"),
        other_url: jpeg(270, 270, b"other"),
    }

    class ImageSession(Session):
        def get(self, url, **kwargs):
            self.calls.append(("GET", url, kwargs))
            if url.endswith("/api/v1/requests/99999991"):
                return Response(payload=payload)
            return Response(
                content=files[url], headers={"Content-Type": "application/octet-stream"}
            )

    provider = CfazProvider(api_token="abc123", session=ImageSession())
    request = provider.discover_request("99999991")[0]
    acquired = provider.download(request, tmp_path, "jpeg-no-extension")

    assert acquired.file_count == 2
    assert [item["stored_name"] for item in acquired.file_metadata] == [
        "imagem_001.jpg", "laudo_imagem_002.jpg",
    ]
    assert {
        key: acquired.file_metadata[0][key]
        for key in (
            "original_source", "stored_name", "detected_mime", "extension",
            "width", "height", "sha256", "collection", "is_thumbnail",
        )
    } == {
        "original_source": "request.images_download_links[1]",
        "stored_name": "imagem_001.jpg",
        "detected_mime": "image/jpeg",
        "extension": ".jpg",
        "width": 1200,
        "height": 800,
        "sha256": hashlib.sha256(files[original_url]).hexdigest(),
        "collection": "request.images_download_links",
        "is_thumbnail": False,
    }
    assert acquired.file_metadata[1]["width"] == 270
    assert acquired.file_metadata[1]["is_thumbnail"] is True
    with zipfile.ZipFile(acquired.archive_path) as archive:
        assert sorted(archive.namelist()) == [
            "imagem_001.jpg", "laudo_imagem_002.jpg",
        ]


def test_generic_service_passes_stable_provider_identity_to_existing_pipeline(tmp_path) -> None:
    files = {"https://files.cfaz.net/panoramica.jpg": b"pan"}
    payload = order_payload()
    payload["images_download_links"] = list(files)
    payload["reports"] = []
    provider = CfazProvider(
        api_token="token", session=Session(request_payload=payload, files=files)
    )

    class Importer:
        def __init__(self): self.calls = []
        def run(self, **kwargs): self.calls.append(kwargs); return "complete"

    importer = Importer()
    output = []
    service = ProviderAcquisitionService(
        provider=provider, importer=importer, quarantine_root=tmp_path,
        correlation_id="correlation-123", output=output.append,
    )
    assert service.run(notification()) == ("complete",)
    call = importer.calls[0]
    assert call["source_provider"] == "cfaz"
    assert call["acquisition_metadata"]["request_id"] == "99999991"
    assert call["acquisition_metadata"]["provider_id"] == "cfaz"
    assert len(call["acquisition_exam_id"]) == 64
    assert Path(call["archive_path"]).is_relative_to(tmp_path)
    assert "Quarentena................... OK" in output


def test_transfernow_adapter_preserves_existing_connector_and_downloader(tmp_path) -> None:
    archive = tmp_path / f"PACIENTE SINTÉTICO_{SYNTHETIC_TIMESTAMP}.zip"
    archive.write_bytes(b"zip")

    class Downloader:
        def download(self, *args):
            return DownloadResult(archive, 3, hashlib.sha256(b"zip").hexdigest())

    message = EmailMessage(
        message_id="message",
        subject=f'TransferNow "PACIENTE SINTÉTICO_{SYNTHETIC_TIMESTAMP}.zip"',
        sender="TransferNow", reply_to=None,
        text_body="fixture2@example.com https://transfernow.net/dl/public-token",
    )
    provider = TransferNowProvider(downloader=Downloader())
    request = provider.discover(message)[0]
    acquired = provider.download(request, tmp_path, "correlation-123")
    assert request.provider_id == "transfernow"
    assert acquired.archive_path == archive
